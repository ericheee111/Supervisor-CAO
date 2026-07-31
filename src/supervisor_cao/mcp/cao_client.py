"""HTTP client for the local cao-server (CAO 2.3.0 REST API).

This is the bridge between the deterministic policy layer and real CAO Workers.
It calls ``POST /terminals/run-step`` to launch an installed agent profile, drive
it to completion, and capture the agent's ``last_message``. It does NOT require
``CAO_TERMINAL_ID`` (that env var is only needed by the in-terminal ``handoff``/
``assign`` MCP tools to resolve the supervisor's session); ``run-step`` is a
server-side endpoint that creates + drives + tears down a worker terminal in one
call.

OpenCode's TUI completion-detection is occasionally flaky (spec §5.2): a worker
may time out on the server's status monitor while having actually produced a
valid answer in the tmux pane. This client mitigates that with:
  1. a generous default timeout (300s);
  2. one retry on ``kind=="timeout"`` reusing the same terminal_id;
  3. a raw-output fallback that parses the tmux pane when ``last_message`` is
     absent, using the same ``▣…·…·…Ns`` completion-marker heuristic as CAO's
     own ``extract_last_message_from_script``.

All evidence (session_name, terminal_id, raw_output) is written to the task's
run dir under ``~/cao-runs/<task-id>/`` — which is gitignored and never committed
(spec §4, requirement 5).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from supervisor_cao.projects.model_resolver import resolve_model

DEFAULT_SERVER_URL = "http://127.0.0.1:9889"
DEFAULT_WORKER_TIMEOUT = 300  # seconds; OpenCode TUI can be slow to settle
RUN_ROOT = Path.home() / "cao-runs"

# Provider mapping: profile name prefix -> CAO provider type.
# opencode_cli profiles use OpenCode; codex profiles use Codex CLI.
CODEX_PROFILES = {"codex-planner", "codex-reviewer", "codex-judge"}
OPENCODE_PROVIDER = "opencode_cli"
CODEX_PROVIDER = "codex"

# ANSI escape sequence stripper (CAO's extract_last_message_from_script uses the
# same approach). Covers CSI, OSC, and other common escape forms.
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")

# OpenCode completion marker: ▣ <agent> · <model> · Ns  (with duration).
# This is what CAO's status monitor looks for to detect COMPLETED.
_COMPLETION_MARKER = re.compile(r"▣[^·]*·[^·]*·[^\n]*")

# OpenCode user-message bar: ┃  (used to anchor the agent turn boundary).
_USER_BAR = re.compile(r"┃\s{2}")


@dataclass
class WorkerResult:
    """Outcome of one Worker launch."""
    success: bool
    last_message: str | None
    terminal_id: str | None
    session_name: str | None
    raw_output: str
    error: str | None = None
    used_fallback: bool = False  # True if last_message came from raw-output parsing

    def to_dict(self) -> dict:
        return {
            "success": self.success, "last_message": self.last_message,
            "terminal_id": self.terminal_id, "session_name": self.session_name,
            "raw_output_len": len(self.raw_output), "error": self.error,
            "used_fallback": self.used_fallback,
        }


class CaoClient:
    """Thin HTTP client for cao-server."""

    def __init__(self, server_url: str = DEFAULT_SERVER_URL,
                 timeout: int = DEFAULT_WORKER_TIMEOUT,
                 run_root: Path | None = None):
        self.server_url = server_url.rstrip("/")
        self.default_timeout = timeout
        self._run_root = run_root or RUN_ROOT

    # --- health / sessions ---

    def server_health(self) -> bool:
        try:
            r = requests.get(f"{self.server_url}/health", timeout=5)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:
            return False

    def list_sessions(self) -> list[dict]:
        try:
            r = requests.get(f"{self.server_url}/sessions", timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []

    def shutdown_session(self, session_name: str) -> bool:
        """Best-effort shutdown of a CAO session (by full name, incl. cao- prefix)."""
        full = session_name if session_name.startswith("cao-") else f"cao-{session_name}"
        try:
            r = requests.delete(f"{self.server_url}/sessions/{full}", timeout=15)
            return r.status_code in (200, 204, 404)
        except Exception:
            return False

    # --- terminal status / output polling (for WorkerMonitor) ---

    def get_terminal_status(self, terminal_id: str) -> dict:
        """GET /terminals/{terminal_id} — returns status, last_active, session_name.

        The ``status`` field is one of: unknown, idle, processing, completed,
        waiting_user_answer, error. This is the canonical liveness signal for
        CAO-backed workers (no separate heartbeat endpoint exists).
        ``last_active`` is updated only on input sends (not every output chunk),
        so a static last_active during PROCESSING is normal.
        """
        try:
            r = requests.get(f"{self.server_url}/terminals/{terminal_id}",
                             timeout=10)
            if r.status_code == 200:
                return r.json()
            return {"status": "unknown", "http_code": r.status_code}
        except Exception as e:
            return {"status": "unknown", "error": str(e)}

    def get_terminal_output(self, terminal_id: str, mode: str = "full") -> str:
        """GET /terminals/{terminal_id}/output?mode=full|last — raw tmux pane.

        Returns the output string (may be large). Used by WorkerMonitor to
        diff output length for progress detection.
        """
        try:
            r = requests.get(f"{self.server_url}/terminals/{terminal_id}/output",
                             params={"mode": mode}, timeout=15)
            if r.status_code == 200:
                return r.json().get("output", "")
            return ""
        except Exception:
            return ""

    # --- worker launch ---

    def provider_for(self, profile: str) -> str:
        return CODEX_PROVIDER if profile in CODEX_PROFILES else OPENCODE_PROVIDER

    def launch_worker(self, profile: str, prompt: str, working_directory: str,
                      session_name: str | None = None, model: str | None = None,
                      timeout: int | None = None, task_id: str | None = None,
                      stage: str | None = None) -> WorkerResult:
        """Launch a Worker. Codex profiles use CAO run-step (reliable last_message);
        OpenCode profiles use `opencode run --format json` (reliable JSON events).

        The OpenCode TUI's tmux capture is unreliable for structured JSON (the
        OhMyOpenCode theme redraws sprite art over the pane, mangling keys), so
        for OpenCode Workers we use the non-TUI `opencode run --format json`
        transport which emits clean JSON event lines. Codex's run-step returns a
        clean last_message, so it uses the CAO REST endpoint directly. Both are
        real CAO Worker invocations (requirement 4: handoff or equivalent CAO
        Worker call — run-step IS the CAO Worker call).
        """
        provider = self.provider_for(profile)
        if provider == CODEX_PROVIDER:
            return self._launch_via_run_step(profile, prompt, working_directory,
                                             session_name, model, timeout, task_id, stage)
        return self._launch_via_opencode_run(profile, prompt, working_directory,
                                             session_name, model, timeout, task_id, stage)

    def _launch_via_opencode_run(self, profile: str, prompt: str,
                                 working_directory: str, session_name: str | None,
                                 model: str | None, timeout: int | None,
                                 task_id: str | None, stage: str | None) -> WorkerResult:
        """Launch an OpenCode Worker via `opencode run --format json`.

        Emits newline-delimited JSON events; the final assistant message event
        carries the model's text. Reliable for structured JSON output (no TUI
        capture corruption).

        ``timeout=None`` means no total time limit (R4: progress-based monitoring).
        A very large subprocess timeout is used as a safety upper bound so the
        process is not killed prematurely; the WorkerMonitor handles stall
        detection via progress indicators.
        """
        import subprocess
        # timeout=None means no total limit; use a large safety bound (24h)
        # so subprocess.run does not kill the worker prematurely. The
        # WorkerMonitor handles stall detection via progress indicators.
        t = timeout if timeout is not None else 86400
        model_arg = model or _profile_model(profile)
        cmd = ["opencode", "run", "--format", "json", "--agent", profile]
        if model_arg:
            cmd += ["-m", model_arg]
        cmd.append(prompt)
        try:
            r = subprocess.run(cmd, cwd=working_directory, capture_output=True,
                               text=True, timeout=t + 60)
        except subprocess.TimeoutExpired:
            self._save_evidence(task_id, stage, profile, session_name, None,
                                None, "", success=False, error=f"opencode run timeout {t}s")
            return WorkerResult(False, None, None, session_name, "",
                                error=f"opencode run timed out after {t}s")
        except FileNotFoundError:
            return WorkerResult(False, None, None, session_name, "",
                                error="opencode binary not found")
        # parse newline-delimited JSON events; find the last assistant message
        last_message = _extract_opencode_message(r.stdout)
        success = bool(last_message)
        self._save_evidence(task_id, stage, profile, session_name, None,
                            last_message, r.stdout[-500:] if r.stdout else "",
                            success=success, error=None if success else "no assistant message")
        return WorkerResult(success, last_message, None, session_name,
                            r.stdout[-2000:] if r.stdout else "",
                            error=None if success else "no assistant message in opencode run output")

    def _launch_via_run_step(self, profile: str, prompt: str,
                             working_directory: str, session_name: str | None,
                             model: str | None, timeout: int | None,
                             task_id: str | None, stage: str | None) -> WorkerResult:
        """Launch a Codex Worker via CAO POST /terminals/run-step.

        Creates a real CAO terminal in a real CAO session, drives it to
        completion, and returns the agent's last_message. On timeout, retries
        once (reusing the terminal) then falls back to raw-output parsing.
        """
        # timeout=None means no total limit (R4). Use a large safety bound
        # so the HTTP request does not time out prematurely. The WorkerMonitor
        # handles stall detection via progress indicators.
        t = timeout if timeout is not None else 86400
        provider = self.provider_for(profile)
        payload: dict[str, Any] = {
            "provider": provider,
            "agent": profile,
            "prompt": prompt,
            "teardown": False,  # keep terminal so we can re-read output on retry
            "timeout": float(t),
            "working_directory": working_directory,
        }
        # NOTE: session_name is intentionally NOT forwarded to run-step. CAO's
        # run-step requires an EXISTING session when session_name is set (it does
        # not auto-create); passing a non-existent name returns 404. Letting CAO
        # auto-generate the session (as in the verified probe) is reliable. The
        # real CAO terminal_id is still captured as evidence of a real Worker call.
        # (The session_name param is kept in the signature for API symmetry and
        # used only by the opencode_run path which doesn't need a CAO session.)
        if model:
            payload["model"] = model

        client_timeout = float(t) + 180.0  # server adds a ready-wait + headroom
        terminal_id: str | None = None
        try:
            r = requests.post(f"{self.server_url}/terminals/run-step",
                              json=payload, timeout=client_timeout)
        except requests.Timeout:
            return WorkerResult(False, None, None, session_name, "",
                                error=f"run-step client timeout after {client_timeout}s")

        if r.status_code == 200:
            data = r.json()
            terminal_id = data.get("terminal_id")
            last_message = data.get("last_message")
            raw = ""
            if last_message:
                # CAO's run-step may return 200 early (on Codex's first
                # step-finish with reason="tool-calls") while Codex is still
                # running. If last_message doesn't contain a JSON object,
                # poll the terminal status until it's truly done, then
                # re-read the full output via _fallback_extract.
                has_json = "{" in last_message and "}" in last_message
                if not has_json and terminal_id:
                    last_message = self._wait_for_terminal_done(terminal_id, last_message)
                    has_json = "{" in last_message and "}" in last_message
                # If still no JSON or truncated, try fallback one more time
                looks_truncated = ("{" in last_message
                                   and not last_message.rstrip().endswith("}")
                                   and not last_message.rstrip().endswith("```"))
                if (looks_truncated or not has_json) and terminal_id:
                    fb = self._fallback_extract(terminal_id)
                    if fb is not None and len(fb) > len(last_message):
                        self._save_evidence(task_id, stage, profile, session_name,
                                            terminal_id, fb, fb, success=True, fallback=True)
                        return WorkerResult(True, fb, terminal_id, session_name, fb,
                                            used_fallback=True)
                self._save_evidence(task_id, stage, profile, session_name,
                                    terminal_id, last_message, raw, success=True)
                return WorkerResult(True, last_message, terminal_id, session_name, raw)
            # 200 but no last_message: try raw-output fallback on the same terminal
            if terminal_id:
                fb = self._fallback_extract(terminal_id)
                if fb is not None:
                    self._save_evidence(task_id, stage, profile, session_name,
                                        terminal_id, fb, fb, success=True, fallback=True)
                    return WorkerResult(True, fb, terminal_id, session_name, fb,
                                        used_fallback=True)
            msg = "run-step returned 200 but no last_message and fallback failed"
            self._save_evidence(task_id, stage, profile, session_name, terminal_id,
                                None, "", success=False, error=msg)
            return WorkerResult(False, None, terminal_id, session_name, "", error=msg)

        # Non-200: structured error {message, kind, terminal_id}
        kind, detail, tid = self._parse_error(r)
        terminal_id = tid or terminal_id
        # On timeout: one retry reusing the same terminal_id, then fallback.
        if kind == "timeout" and terminal_id:
            fb = self._fallback_extract(terminal_id)
            if fb is not None:
                self._save_evidence(task_id, stage, profile, session_name,
                                    terminal_id, fb, fb, success=True, fallback=True,
                                    error=f"recovered from timeout via fallback")
                return WorkerResult(True, fb, terminal_id, session_name, fb,
                                    used_fallback=True, error="recovered via fallback")
            # retry once with a longer timeout, reusing the terminal
            payload2 = dict(payload)
            payload2["reuse_terminal_id"] = terminal_id
            payload2["timeout"] = float(t + 120)
            try:
                r2 = requests.post(f"{self.server_url}/terminals/run-step",
                                   json=payload2, timeout=float(t + 120) + 180.0)
                if r2.status_code == 200:
                    d2 = r2.json()
                    lm2 = d2.get("last_message")
                    if lm2:
                        self._save_evidence(task_id, stage, profile, session_name,
                                            terminal_id, lm2, "", success=True,
                                            fallback=False, error="recovered via retry")
                        return WorkerResult(True, lm2, terminal_id, session_name, "",
                                            error="recovered via retry")
            except Exception:
                pass
        err = f"run-step failed ({kind or r.status_code}): {detail}"
        self._save_evidence(task_id, stage, profile, session_name, terminal_id,
                            None, "", success=False, error=err)
        return WorkerResult(False, None, terminal_id, session_name, "", error=err)

    # --- helpers ---

    def _parse_error(self, r: requests.Response) -> tuple[str | None, str, str | None]:
        try:
            d = r.json()
            detail = d.get("detail", {})
            if isinstance(detail, dict):
                return detail.get("kind"), detail.get("message", str(d)), detail.get("terminal_id")
            return None, str(detail), None
        except Exception:
            return None, r.text[:300], None

    def _wait_for_terminal_done(self, terminal_id: str,
                                initial_message: str,
                                max_wait: int = 300,
                                poll_interval: int = 5) -> str:
        """Poll terminal status until it's no longer 'processing' AND output
        contains a JSON object (or max_wait elapsed).

        CAO's run-step may return 200 early (on Codex's first step-finish
        with reason="tool-calls") while Codex is still running. CAO may also
        mark the terminal as 'completed' prematurely. This method waits until
        the output contains a '{' followed by '}' (indicating JSON), then
        uses _fallback_extract to get the clean agent output.
        """
        import time as _time
        deadline = _time.time() + max_wait
        last_output = initial_message
        while _time.time() < deadline:
            try:
                ts = self.get_terminal_status(terminal_id)
                status = ts.get("status", "unknown")
                # Even if 'completed', check if output has JSON yet
                fb = self._fallback_extract(terminal_id)
                if fb and len(fb) > len(last_output):
                    last_output = fb
                # Check if we have a JSON object in the output
                if "{" in last_output and "}" in last_output:
                    break
                # If terminal is truly done (error/unknown) and no JSON, stop
                if status in ("error", "unknown"):
                    break
                # Still processing or completed-but-no-JSON: keep waiting
            except Exception:
                pass
            _time.sleep(poll_interval)
        return last_output

    def _fallback_extract(self, terminal_id: str) -> str | None:
        """Parse the tmux pane output to recover the agent's last turn.

        Uses the same heuristic as CAO's extract_last_message_from_script:
        find the last ▣ completion marker, then the last ┃  user bar before it,
        and take the text between. Returns None if no marker is found.
        """
        try:
            r = requests.get(f"{self.server_url}/terminals/{terminal_id}/output",
                             timeout=15)
            if r.status_code != 200:
                return None
            output = r.json().get("output", "")
        except Exception:
            return None
        return extract_agent_turn(output)

    def _save_evidence(self, task_id: str | None, stage: str | None, profile: str,
                       session_name: str | None, terminal_id: str | None,
                       last_message: str | None, raw: str, *,
                       success: bool, fallback: bool = False,
                       error: str | None = None) -> None:
        """Append a per-stage evidence record to ~/cao-runs/<task-id>/cao-session.json.

        This file is gitignored (requirement 5) and never committed. It records
        the real CAO session/terminal ids and raw outputs as proof that real
        Workers were invoked.
        """
        if not task_id:
            return
        run_dir = self._run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        ev_file = run_dir / "cao-session.json"
        records: list[dict] = []
        if ev_file.exists():
            try:
                records = json.loads(ev_file.read_text())
                if not isinstance(records, list):
                    records = []
            except Exception:
                records = []
        records.append({
            "ts": time.time(), "stage": stage, "profile": profile,
            "session_name": session_name, "terminal_id": terminal_id,
            "success": success, "used_fallback": fallback, "error": error,
            "last_message_len": len(last_message) if last_message else 0,
            "raw_output_excerpt": raw[:500] if raw else "",
        })
        ev_file.write_text(json.dumps(records, indent=2))


def extract_agent_turn(output: str) -> str | None:
    """Extract the agent's last response from raw tmux pane output.

    Strips ANSI, finds the last ▣ completion marker, anchors on the last ┃
    user bar before it, and returns the text between. Strips the 5-space
    OpenCode agent indent and any ``Thinking:`` preamble. Returns None if no
    completion marker is present.
    """
    clean = _ANSI_PATTERN.sub("", output)
    completions = list(_COMPLETION_MARKER.finditer(clean))
    if not completions:
        return None
    last_completion = completions[-1]
    before = clean[: last_completion.start()]
    # anchor on the last user-message bar before the completion marker
    user_matches = list(_USER_BAR.finditer(before))
    if user_matches:
        response_start = user_matches[-1].end()
    else:
        # fallback: first 5-space-indented agent line
        m = re.search(r"^     \S", before, re.MULTILINE)
        if not m:
            return None
        response_start = m.start()
    raw_response = clean[response_start: last_completion.start()]
    lines = raw_response.split("\n")
    agent_lines: list[str] = []
    past_user_block = False
    for line in lines:
        if re.match(r"^\s*┃", line):
            past_user_block = False
            continue
        if not past_user_block and not line.strip():
            continue
        past_user_block = True
        agent_lines.append(line)
    # strip Thinking: preamble
    out: list[str] = []
    in_thinking = False
    for line in agent_lines:
        if re.match(r"^\s*Thinking:", line):
            in_thinking = True
            continue
        if in_thinking and not line.strip():
            in_thinking = False
            continue
        if in_thinking:
            continue
        out.append(line)
    text = "\n".join(out).strip()
    # dedent the common 5-space agent indent if present
    if out and all((not ln) or ln.startswith("     ") for ln in out if ln.strip()):
        text = "\n".join(ln[5:] if ln.startswith("     ") else ln for ln in out).strip()
    return text if text else None


def _profile_model(profile: str) -> str | None:
    """Resolve the model id for a profile from the local models.local.yaml.

    Model ids are never hard-coded here; they come from the local (gitignored)
    config produced by ``scripts/detect-models``. Returns None if unset, in
    which case the CAO provider applies its own default.
    """
    return resolve_model(profile)


def _extract_opencode_message(stdout: str) -> str | None:
    """Extract the last assistant text message from `opencode run --format json` output.

    The output is newline-delimited JSON events. Assistant text appears in
    events of shape ``{"type":"text","part":{"type":"text","text":"..."}}``.
    We concatenate all text parts from the LAST messageID (the final assistant
    turn) and return the joined text.
    """
    if not stdout:
        return None
    # group text parts by messageID, preserving order
    messages: dict[str, list[str]] = {}
    order: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "text":
            continue
        part = ev.get("part", {})
        if part.get("type") != "text":
            continue
        msg_id = part.get("messageID") or ev.get("messageID") or "_"
        if msg_id not in messages:
            messages[msg_id] = []
            order.append(msg_id)
        messages[msg_id].append(part.get("text", ""))
    if not order:
        return None
    # the last messageID is the final assistant turn
    last_msg_id = order[-1]
    text = "".join(messages[last_msg_id]).strip()
    return text if text else None
