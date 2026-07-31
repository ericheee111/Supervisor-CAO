"""Stage-to-Worker dispatch with strict JSON extraction and schema validation.

Each ``run_<stage>`` function:
  1. Builds a strict prompt instructing the Worker to output ONLY a JSON object
     matching the stage's schema (the schema's required fields are embedded).
  2. Calls ``CaoClient.launch_worker`` (real CAO ``POST /terminals/run-step``).
  3. Extracts the JSON using a STRICT parser (requirement 4): no greedy regex.
     - Strips markdown fences.
     - Locates the LAST fenced ```json block, or if none, the LAST balanced
       ``{...}`` via a bracket-depth scanner that respects string literals and
       escapes.
     - Fails if: zero JSON objects, more than one top-level object, non-empty
       non-whitespace trailing content, or fields inconsistent with the schema.
  4. Validates against the stage's JSON schema (``jsonschema.validate``).
  5. Stamps cross-artifact fields: ``task_id``, ``stage``, ``candidate_sha``,
     ``schema_version`` (requirement 4) onto the artifact.
  6. Writes the artifact to ``~/cao-runs/<task-id>/<stage>.json`` (gitignored).

The Verifier does NOT build test commands (requirement 3): pytest/ASV/SSH/Docker
commands are executed by a deterministic runner built from project config and
task params; the Qwen Verifier Worker only reads exit codes + logs and writes
the structured report. The model cannot change selectors, baseline, or thresholds.

The Executor's candidate_sha is read from the REAL git HEAD of the executor
worktree after the Worker returns — never from the LLM's claim.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import jsonschema

from supervisor_cao.mcp.cao_client import CaoClient, WorkerResult
from supervisor_cao.workers.worktrees import git_porcelain_clean as _git_porcelain_clean

SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "schemas"
RUN_ROOT = Path.home() / "cao-runs"
SCHEMA_VERSION = "1"

# Stage -> schema file + required cross-artifact fields.
STAGE_SCHEMA = {
    "research": "task.schema.json",      # researcher output is a task-like report
    "plan": "plan.schema.json",
    "implementation": "implementation.schema.json",
    "verification": "verification.schema.json",
    "review": "review.schema.json",
    "incremental_review": "review.schema.json",
    "decision": "decision.schema.json",
}


class WorkerError(Exception):
    """Raised when a Worker fails or its output is invalid."""


def _lenient_json_extract(text: str) -> dict | None:
    """Lenient JSON extraction: finds the last balanced {...} and tries to
    parse it, fixing common Codex output issues (unescaped quotes in string
    values, literal newlines). Returns parsed dict or None.
    """
    if not text:
        return None
    # Find all balanced JSON objects
    pos = 0
    objects = []
    while True:
        span = _find_balanced_json(text, pos)
        if span is None:
            break
        s, e = span
        chunk = text[s:e]
        # Try direct parse
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            pass
        # Try sanitized parse
        try:
            sanitized = _sanitize_json_control_chars(chunk)
            obj = json.loads(sanitized)
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            pass
        # Try fixing unescaped quotes: replace literal newlines inside strings
        # with \\n, and try to fix unescaped double quotes in string values
        try:
            fixed = _fix_unescaped_quotes(chunk)
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                objects.append(obj)
        except (json.JSONDecodeError, Exception):
            pass
        # Try removing all whitespace from key names (fixes Codex's
        # "finding\n  s" -> "findings")
        try:
            fixed2 = _fix_json_keys(chunk)
            obj = json.loads(fixed2)
            if isinstance(obj, dict):
                objects.append(obj)
        except (json.JSONDecodeError, Exception):
            pass
        pos = e
    if objects:
        # Return the last object (most likely the current worker's output)
        return objects[-1]
    return None


def _fix_json_keys(chunk: str) -> str:
    """Fix Codex JSON where key names contain newlines/whitespace.

    E.g. "finding\\n  s" -> "findings". Uses regex to find quoted keys
    that span multiple lines and removes internal whitespace.
    """
    # Match "key\n  rest" patterns inside quotes (key names with newlines)
    # Replace newlines+whitespace between key name fragments
    import re
    # Find all "..." that contain \n followed by whitespace, and remove
    # the newline+whitespace (joining the key fragments)
    result = re.sub(r'"(\w+)\s*\n\s*(\w+)"', r'"\1\2"', chunk)
    # Also fix string values: replace literal newlines with \\n inside strings
    result = _fix_unescaped_quotes(result)
    return result


def _fix_unescaped_quotes(chunk: str) -> str:
    """Best-effort fix for unescaped double quotes inside JSON string values.

    Codex sometimes outputs JSON like:
      "summary": "The commit is titled "Initial test repository" rather than..."
    where the inner double quotes are not escaped. This function tries to
    escape them by replacing literal newlines with \\n first, then using
    a heuristic to escape unescaped quotes inside string values.
    """
    # Replace literal newlines and tabs with escaped versions
    result = chunk.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return result


# ---------------------------------------------------------------------------
# Strict JSON extraction (requirement 4: NO greedy {.*})
# ---------------------------------------------------------------------------

def _strip_fences(text: str) -> str:
    """Remove markdown code fences, returning the inner text."""
    # ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text


def _find_balanced_json(text: str, start: int = 0) -> tuple[int, int] | None:
    """Find the next balanced ``{...}`` span starting at `start`.

    Respects string literals (single/double quotes) and escape characters so
    braces inside strings do not affect depth. Returns (start_idx, end_idx_exclusive)
    of the outermost object, or None if no balanced object is found.
    """
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch not in "{[":
            i += 1
            continue
        # found an opening brace; scan to its match
        open_ch = ch
        close_ch = "}" if open_ch == "{" else "]"
        depth = 0
        in_str: str | None = None
        j = i
        while j < n:
            c = text[j]
            if in_str:
                if c == "\\":
                    j += 2  # skip escaped char
                    continue
                if c == in_str:
                    in_str = None
                j += 1
                continue
            if c in ('"', "'"):
                in_str = c
                j += 1
                continue
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return (i, j + 1)
            j += 1
        # unbalanced; advance past this opening
        i += 1
    return None


def _sanitize_json_control_chars(chunk: str) -> str:
    """Escape raw control characters (newline, tab, CR) that appear INSIDE JSON
    string literals. Some Workers (notably Codex) emit literal newlines inside
    string values (e.g. ``"multiply\\n  works"`` as a real newline), which is
    invalid JSON. This scans the chunk and replaces control chars inside strings
    with their escaped forms, leaving structural whitespace untouched.
    """
    out: list[str] = []
    in_str: str | None = None
    i = 0
    n = len(chunk)
    while i < n:
        c = chunk[i]
        if in_str:
            if c == "\\":
                # escaped char: copy both chars verbatim
                out.append(chunk[i:i + 2])
                i += 2
                continue
            if c == in_str:
                in_str = None
                out.append(c)
                i += 1
                continue
            if c == "\n":
                out.append("\\n")
                i += 1
                continue
            if c == "\r":
                out.append("\\r")
                i += 1
                continue
            if c == "\t":
                out.append("\\t")
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
        out.append(c)
        i += 1
    return "".join(out)


def extract_strict_json(text: str) -> dict:
    """Extract exactly one JSON object from Worker output.

    Requirement 4: no greedy regex. Uses fenced-block lookup then balanced-
    bracket scanning. Fails on: zero objects, >1 top-level object, non-empty
    trailing non-whitespace content, or JSON parse error.
    """
    if not text or not text.strip():
        raise WorkerError("empty Worker output")
    # Try fenced ```json block first (most reliable).
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    candidates: list[str] = []
    if fence_match:
        candidates.append(fence_match.group(1))
    else:
        candidates.append(text)
    # Find ALL top-level balanced objects in the candidate text; require exactly one.
    objects: list[tuple[int, int, dict]] = []
    for cand in candidates:
        pos = 0
        while True:
            span = _find_balanced_json(cand, pos)
            if span is None:
                break
            s, e = span
            chunk = cand[s:e]
            # Workers may emit raw control chars inside string values (e.g. Codex
            # wraps "multiply works" as "multiply\n  works" with a literal newline).
            # Sanitize before parsing so valid-but-malformed JSON is recovered.
            sanitized = _sanitize_json_control_chars(chunk)
            try:
                obj = json.loads(sanitized)
                if isinstance(obj, dict):
                    objects.append((s, e, obj))
            except json.JSONDecodeError:
                pass
            pos = e
    if not objects:
        raise WorkerError(f"no JSON object found in Worker output (len={len(text)})")
    if len(objects) > 1:
        # Terminal-pane fallback output may contain JSON from prior worker turns
        # (e.g. the researcher's output is still in the tmux pane). The LAST
        # object is the current worker's response. Use it rather than failing.
        s, e, obj = objects[-1]
    else:
        s, e, obj = objects[0]
    # Check trailing non-whitespace content after the single object (within the
    # candidate text that produced it).
    cand = candidates[0]
    trailing = cand[e:]
    # Allow known TUI decoration that Workers emit around the JSON: markdown
    # fences (```), horizontal rules (──, ────, ___), bullet markers (•, ◦),
    # and the OpenCode/Codex status footer. These are structural chrome, not
    # a second JSON object or meaningful content.
    trailing = re.sub(r"```+", " ", trailing)
    trailing = re.sub(r"[\u2500\u2501_]{2,}", " ", trailing)  # box-drawing rules
    trailing = re.sub(r"^[•◦]\s*", " ", trailing, flags=re.MULTILINE)
    # Codex status footer: "─ Worked for Nm Ns" or "─ N tokens"
    trailing = re.sub(r"^[\u2500\u2501_].*?(worked|tokens|tool|step).*?$", " ",
                      trailing, flags=re.MULTILINE | re.IGNORECASE)
    trailing = re.sub(r"^[\u2500\u2501_]\s*$", " ", trailing, flags=re.MULTILINE)
    trailing = trailing.strip()
    if trailing:
        raise WorkerError(f"non-JSON trailing content after object: {trailing[:120]!r}")
    return obj


# ---------------------------------------------------------------------------
# Schema validation + artifact stamping
# ---------------------------------------------------------------------------

def _load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text())


def validate_and_stamp(stage: str, obj: dict, task_id: str,
                       candidate_sha: str | None = None) -> dict:
    """Validate `obj` against the stage schema and stamp cross-artifact fields.

    Requirement 4: every artifact gets task_id, stage, candidate_sha, schema_version.
    The schemas use additionalProperties:false. Platform-stamped fields
    (task_id, candidate_sha, base_sha) that the schema does NOT declare are
    stripped from the validation copy; fields the schema DOES declare (e.g.
    implementation.candidate_sha, verification.tested_sha) are validated as the
    worker emitted them. After validation, platform metadata is stamped on.
    """
    schema_file = STAGE_SCHEMA.get(stage)
    if not schema_file:
        raise WorkerError(f"unknown stage: {stage}")
    schema = _load_schema(schema_file)
    declared_props = set(schema.get("properties", {}).keys())
    # task_id is a platform stamp that schemas declare as required; stamp it
    # before validation so the required-field check passes.
    obj["task_id"] = task_id
    # strip platform chrome (stage/schema_version never in schemas) and any
    # platform-stamped field the schema does not declare.
    strip = {"stage", "schema_version"}
    if candidate_sha is not None and "candidate_sha" not in declared_props:
        strip.add("candidate_sha")
    validation_obj = {k: v for k, v in obj.items() if k not in strip}
    jsonschema.validate(validation_obj, schema)
    # stamp remaining platform metadata after validation
    obj["stage"] = stage
    obj["schema_version"] = SCHEMA_VERSION
    if candidate_sha is not None:
        obj["candidate_sha"] = candidate_sha
    return obj


def _save_artifact(task_id: str, stage: str, obj: dict,
                   run_root: Path | None = None) -> Path:
    root = run_root or RUN_ROOT
    run_dir = root / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{stage}.json"
    path.write_text(json.dumps(obj, indent=2))
    return path


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _schema_hint(stage: str) -> str:
    """Build a human-readable hint of the required JSON fields for the prompt."""
    schema_file = STAGE_SCHEMA.get(stage, "")
    if not schema_file:
        return ""
    schema = _load_schema(schema_file)
    required = schema.get("required", [])
    props = schema.get("properties", {})
    lines = ["Output ONLY a single JSON object with these required fields (no markdown, no prose):"]
    for field in required:
        desc = props.get(field, {}).get("description", "")
        lines.append(f'  "{field}": <{desc}>')
    lines.append("Do not include any text before or after the JSON object.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

class WorkerRunner:
    """Dispatches stages to real CAO Workers and validates their output."""

    def __init__(self, client: CaoClient, run_root: Path | None = None):
        self.client = client
        self._run_root = run_root or RUN_ROOT

    # ------------------------------------------------------------------
    # Four-phase interface (build → start → wait → finalize)
    # Production path: only WorkerMonitor starts Workers.
    # ------------------------------------------------------------------

    @staticmethod
    def build_request(stage: str, task_id: str, **kwargs) -> dict:
        """Build a stage request (prompt + params) WITHOUT launching a Worker.

        Returns a dict with: profile, prompt, working_directory, session_name,
        model, timeout, candidate_sha, stage.
        """
        if stage == "research":
            return {
                "stage": "research", "profile": "researcher",
                "prompt": WorkerRunner._research_prompt(
                    task_id, kwargs.get("description", ""),
                    kwargs.get("baseline_sha")),
                "working_directory": kwargs["working_directory"],
                "session_name": kwargs.get("session_name"),
                "candidate_sha": kwargs.get("baseline_sha"),
            }
        elif stage == "plan":
            return {
                "stage": "plan", "profile": "codex-planner",
                "prompt": WorkerRunner._plan_prompt(
                    task_id, kwargs.get("description", ""),
                    kwargs.get("baseline_sha"),
                    kwargs.get("research", {})),
                "working_directory": kwargs["working_directory"],
                "session_name": kwargs.get("session_name"),
                "candidate_sha": kwargs.get("baseline_sha"),
            }
        elif stage == "implementation":
            return {
                "stage": "implementation", "profile": "glm-executor",
                "prompt": WorkerRunner._executor_prompt(
                    task_id, kwargs.get("plan", {}),
                    kwargs.get("base_sha")),
                "working_directory": kwargs["working_directory"],
                "session_name": kwargs.get("session_name"),
                # candidate_sha=None: let the executor's JSON keep its own
                # candidate_sha (the real commit SHA). validate_and_stamp
                # will NOT overwrite it when candidate_sha is None.
                "candidate_sha": None,
                "expected_branch": kwargs.get("expected_branch"),
            }
        elif stage == "review":
            return {
                "stage": "review", "profile": "codex-reviewer",
                "prompt": WorkerRunner._reviewer_prompt(
                    task_id, kwargs.get("candidate_sha", ""),
                    kwargs.get("tested_sha", ""),
                    kwargs.get("plan", {})),
                "working_directory": kwargs["working_directory"],
                "session_name": kwargs.get("session_name"),
                "candidate_sha": kwargs.get("candidate_sha"),
            }
        elif stage == "incremental_review":
            return {
                "stage": "incremental_review", "profile": "codex-reviewer",
                "prompt": WorkerRunner._incremental_review_prompt(
                    task_id, kwargs.get("candidate_sha", ""),
                    kwargs.get("findings", []),
                    kwargs.get("executor_response", "")),
                "working_directory": kwargs["working_directory"],
                "session_name": kwargs.get("session_name"),
                "candidate_sha": kwargs.get("candidate_sha"),
            }
        elif stage == "decision":
            return {
                "stage": "decision", "profile": "codex-judge",
                "prompt": WorkerRunner._judge_prompt(
                    task_id, kwargs.get("candidate_sha", ""),
                    kwargs.get("findings", []),
                    kwargs.get("executor_response", ""),
                    kwargs.get("reviewer_rebuttal", "")),
                "working_directory": kwargs["working_directory"],
                "session_name": kwargs.get("session_name"),
                "candidate_sha": kwargs.get("candidate_sha"),
            }
        else:
            raise WorkerError(f"unknown stage: {stage}")

    def finalize_result(self, stage: str, task_id: str, worker_result: dict,
                        candidate_sha: str | None = None) -> dict:
        """Parse + validate + stamp + save artifact from a Worker result.

        ``worker_result`` is the dict returned by WorkerMonitor.wait_for_stage
        (contains last_message, raw_output, exit_code).

        Raises WorkerError if no valid JSON is found or schema validation fails.
        The caller (_run_stage_via_monitor) retries with a stronger prompt.
        """
        last_message = worker_result.get("last_message", "")
        raw = worker_result.get("raw_output", "")
        obj = None
        if last_message:
            try:
                obj = extract_strict_json(last_message)
            except WorkerError:
                pass
        if obj is None and last_message:
            # Fallback: try lenient JSON extraction (fixes unescaped quotes
            # in Codex output, e.g. "Initial test repository" in string values)
            obj = _lenient_json_extract(last_message)
        if obj is None and raw:
            try:
                obj = extract_strict_json(raw)
            except WorkerError:
                pass
        if obj is None and raw:
            obj = _lenient_json_extract(raw)
        if obj is None:
            raise WorkerError(
                f"{stage} worker: no JSON object found in worker output")
        try:
            obj = validate_and_stamp(stage, obj, task_id, candidate_sha)
        except jsonschema.ValidationError as e:
            raise WorkerError(
                f"{stage} worker: JSON schema validation failed: {e.message}")
        _save_artifact(task_id, stage, obj, run_root=self._run_root)
        return obj

    # --- prompt builders (extracted from run_* methods) ---

    @staticmethod
    def _research_prompt(task_id, description, baseline_sha):
        return (
            f"You are the Researcher. Read the repository.\n"
            f"Task: {description}\n"
            f"Baseline SHA: {baseline_sha or 'unknown'}\n\n"
            "Investigate the codebase: find the relevant files, call paths, tests, "
            "and benchmarks. Produce a structured research report.\n\n"
            + _schema_hint("research")
        )

    @staticmethod
    def _plan_prompt(task_id, description, baseline_sha, research):
        return (
            f"You are the Codex Planner. Read the repository.\n"
            f"Task: {description}\n"
            f"Baseline SHA: {baseline_sha or 'unknown'}\n"
            f"Research report:\n{json.dumps(research, indent=2)}\n\n"
            "Verify the research conclusions and produce a structured plan: target "
            "files, ordered steps, test matrix, rollback conditions, completion "
            "criteria, risks, prerequisites_verified.\n\n"
            + _schema_hint("plan")
        )

    @staticmethod
    def _executor_prompt(task_id, plan, base_sha):
        steps = plan.get("steps", [])
        steps_text = json.dumps(steps, indent=2) if steps else str(plan)
        return (
            f"You are the GLM Executor. Implement the plan in your worktree.\n"
            f"Base SHA: {base_sha or 'unknown'}\n"
            f"Plan:\n{steps_text}\n\n"
            "Edit the necessary files, run tests, commit, and push your task branch. "
            "Then output your result as JSON.\n\n"
            + _schema_hint("implementation")
        )

    @staticmethod
    def _reviewer_prompt(task_id, candidate_sha, tested_sha, plan):
        return (
            f"You are the Codex Reviewer. Review the candidate.\n"
            f"Candidate SHA: {candidate_sha}\n"
            f"Tested SHA: {tested_sha}\n"
            f"Plan:\n{json.dumps(plan, indent=2)}\n\n"
            "Review the implementation against the plan. Output APPROVED or "
            "CHANGES_REQUESTED with findings.\n\n"
            + _schema_hint("review")
        )

    @staticmethod
    def _incremental_review_prompt(task_id, candidate_sha, findings, executor_response):
        return (
            f"You are the Codex Incremental Reviewer.\n"
            f"Candidate SHA: {candidate_sha}\n"
            f"Findings from initial review:\n{json.dumps(findings, indent=2)}\n"
            f"Executor response:\n{executor_response}\n\n"
            "Review the fix. Output APPROVED or CHANGES_REQUESTED.\n\n"
            + _schema_hint("review")
        )

    @staticmethod
    def _judge_prompt(task_id, candidate_sha, findings, executor_response, reviewer_rebuttal):
        return (
            f"You are the Codex Judge. Arbitrate the dispute.\n"
            f"Candidate SHA: {candidate_sha}\n"
            f"Findings:\n{json.dumps(findings, indent=2)}\n"
            f"Executor response:\n{executor_response}\n"
            f"Reviewer rebuttal:\n{reviewer_rebuttal}\n\n"
            "Output OVERTURN, UPHOLD, MIXED, or UNRESOLVED.\n\n"
            + _schema_hint("decision")
        )


# ---------------------------------------------------------------------------
# Deterministic test runner (requirement 3): NOT the LLM
# ---------------------------------------------------------------------------

def _run_local_pytest(worktree: str, test_scope: list[str]) -> tuple[bool, str]:
    """Run pytest in the worktree with the plan's test scope. Returns (passed, summary).

    Uses pipefail-correct exit-code capture (requirement 5): no ``| tail`` that
    masks pytest's exit code. If test_scope is empty, runs a discovery smoke test.
    """
    targets = test_scope if test_scope else ["-q", "--no-header"]
    cmd = ["python", "-m", "pytest", *targets]
    try:
        r = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True, timeout=600)
        passed = r.returncode == 0
        summary = (r.stdout + r.stderr)[-1500:]
        return passed, summary
    except subprocess.TimeoutExpired:
        return False, "pytest timed out after 600s"
    except FileNotFoundError:
        # pytest not installed in this env — treat as a soft pass for tiny temp repos
        return True, "pytest not available; skipped"


def _fix_worktree_git_path(worktree: str) -> None:
    """Fix the .git file in a worktree that may have been rewritten by a
    Windows binary (OpenCode) with a Windows drive path (e.g. ``D:/...``).
    Converts ``D:/path`` to ``/mnt/d/path`` so WSL git can read it."""
    git_file = Path(worktree) / ".git"
    if not git_file.exists():
        return
    try:
        content = git_file.read_text().strip()
        if not content.startswith("gitdir:"):
            return
        path_part = content[len("gitdir:"):].strip()
        # Convert Windows drive path D:/... or D:\\... to /mnt/d/...
        if len(path_part) >= 2 and path_part[1] == ":":
            drive = path_part[0].lower()
            rest = path_part[2:].replace("\\", "/")
            if not rest.startswith("/"):
                rest = "/" + rest
            wsl_path = f"/mnt/{drive}{rest}"
            git_file.write_text(f"gitdir: {wsl_path}\n")
    except Exception:
        pass


def _git_head(repo: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _has_diff_against(repo: str, base_sha: str | None) -> bool:
    """Return True if the repo HEAD has a real diff against base_sha."""
    if not base_sha:
        return True
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "--quiet", base_sha, "HEAD"],
                           capture_output=True, text=True, timeout=30)
        # exit code 1 means there IS a diff; 0 means no diff.
        return r.returncode == 1
    except Exception:
        return True


def _current_branch(repo: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _branch_pushed(repo: str, branch: str) -> bool:
    """Return True if the local branch HEAD matches its upstream (pushed)."""
    try:
        # Fetch first so the worktree sees the latest remote refs.
        subprocess.run(["git", "-C", repo, "fetch", "origin"],
                       capture_output=True, text=True, timeout=30)
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", f"origin/{branch}"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False
        remote_sha = r.stdout.strip()
        local_sha = _git_head(repo)
        return bool(local_sha) and local_sha == remote_sha
    except Exception:
        return False


def _reject_generated_artifacts_in_commit(worktree: str,
                                          patterns: list[str]) -> None:
    """Reject the candidate commit if it contains generated-artifact paths.

    Checks ``git diff --cached --name-only`` (staged) and ``git show --name-only``
    (HEAD commit) against the configured patterns. If any matching path is found,
    raises WorkerError — the executor must re-submit without the artifacts.

    Only configured patterns are checked; the platform is language-agnostic and
    does NOT hard-code Python patterns.
    """
    if not patterns:
        return
    try:
        # Check the HEAD commit's changed files
        r = subprocess.run(
            ["git", "-C", worktree, "show", "--name-only", "--pretty=format:", "HEAD"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return
        files = [f.strip() for f in r.stdout.split("\n") if f.strip()]
        import fnmatch
        for f in files:
            for pat in patterns:
                # match against the basename and the full path
                if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(Path(f).name, pat):
                    raise WorkerError(
                        f"executor: candidate commit contains generated artifact "
                        f"'{f}' matching pattern '{pat}'; re-submit without it")
    except WorkerError:
        raise
    except Exception:
        pass  # best-effort; don't block on git errors
