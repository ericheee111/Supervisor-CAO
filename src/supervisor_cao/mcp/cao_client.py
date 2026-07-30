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

DEFAULT_SERVER_URL = "http://127.0.0.1:9889"
DEFAULT_WORKER_TIMEOUT = 300  # seconds; OpenCode TUI can be slow to settle
RUN_ROOT = Path.home() / "cao-runs"

# Provider mapping: profile name prefix -> CAO provider type.
# opencode_cli profiles use OpenCode (GLM/Qwen); codex profiles use Codex CLI.
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

    # --- worker launch ---

    def provider_for(self, profile: str) -> str:
        return CODEX_PROVIDER if profile in CODEX_PROFILES else OPENCODE_PROVIDER

    def launch_worker(self, profile: str, prompt: str, working_directory: str,
                      session_name: str | None = None, model: str | None = None,
                      timeout: int | None = None, task_id: str | None = None,
                      stage: str | None = None) -> WorkerResult:
        """Launch a Worker via POST /terminals/run-step.

        Creates a real CAO terminal in a real CAO session, drives it to
        completion, and returns the agent's last_message. On timeout, retries
        once (reusing the terminal) then falls back to raw-output parsing.
        """
        t = timeout or self.default_timeout
        provider = self.provider_for(profile)
        payload: dict[str, Any] = {
            "provider": provider,
            "agent": profile,
            "prompt": prompt,
            "teardown": False,  # keep terminal so we can re-read output on retry
            "timeout": float(t),
            "working_directory": working_directory,
        }
        if session_name:
            payload["session_name"] = session_name
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
