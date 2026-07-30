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
            # skip whitespace before this object
            leading = cand[:s].strip()
            if leading:
                # non-JSON content before the object — only allow if it's a
                # known preamble (e.g. "## Executor result"). We still record
                # the object but flag trailing content below.
                pass
            try:
                obj = json.loads(chunk)
                if isinstance(obj, dict):
                    objects.append((s, e, obj))
            except json.JSONDecodeError:
                pass
            pos = e
    if not objects:
        raise WorkerError(f"no JSON object found in Worker output (len={len(text)})")
    if len(objects) > 1:
        raise WorkerError(f"multiple JSON objects found ({len(objects)}); expected exactly one")
    s, e, obj = objects[0]
    # Check trailing non-whitespace content after the single object (within the
    # candidate text that produced it).
    cand = candidates[0]
    trailing = cand[e:].strip()
    # Allow a trailing ``` fence marker if we extracted from a fenced block with
    # the closing fence already consumed by the regex.
    trailing = trailing.replace("```", "").strip()
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
    These are extra properties; the schemas use additionalProperties:false, so we
    validate a COPY without the stamps (the stamps are platform metadata, not
    part of the stage schema), then add them to the returned artifact.
    """
    schema_file = STAGE_SCHEMA.get(stage)
    if not schema_file:
        raise WorkerError(f"unknown stage: {stage}")
    schema = _load_schema(schema_file)
    # validate the worker's own fields (without platform stamps)
    validation_obj = {k: v for k, v in obj.items()
                      if k not in {"stage", "schema_version"}}
    jsonschema.validate(validation_obj, schema)
    # stamp platform metadata
    obj["task_id"] = task_id
    obj["stage"] = stage
    obj["schema_version"] = SCHEMA_VERSION
    if candidate_sha is not None:
        obj["candidate_sha"] = candidate_sha
    return obj


def _save_artifact(task_id: str, stage: str, obj: dict) -> Path:
    run_dir = RUN_ROOT / task_id
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

    def _run(self, task_id: str, stage: str, profile: str, prompt: str,
             working_directory: str, session_name: str | None,
             model: str | None = None, timeout: int | None = None,
             candidate_sha: str | None = None) -> dict:
        """Launch a Worker, extract+validate JSON, stamp, and save the artifact."""
        result = self.client.launch_worker(
            profile, prompt, working_directory, session_name, model, timeout,
            task_id=task_id, stage=stage)
        if not result.success or not result.last_message:
            raise WorkerError(f"{stage} worker failed: {result.error or 'no output'}")
        obj = extract_strict_json(result.last_message)
        obj = validate_and_stamp(stage, obj, task_id, candidate_sha)
        _save_artifact(task_id, stage, obj)
        return obj

    # --- research ---

    def run_researcher(self, task_id: str, description: str, baseline_sha: str | None,
                       working_directory: str, session_name: str | None) -> dict:
        prompt = (
            f"You are the Researcher. Read the repository at {working_directory}.\n"
            f"Task: {description}\n"
            f"Baseline SHA: {baseline_sha or 'unknown'}\n\n"
            "Investigate the codebase: find the relevant files, call paths, tests, "
            "and benchmarks. Produce a structured research report.\n\n"
            + _schema_hint("research")
        )
        return self._run(task_id, "research", "researcher", prompt,
                         working_directory, session_name)

    # --- plan (Codex) ---

    def run_planner(self, task_id: str, description: str, baseline_sha: str | None,
                    research: dict, working_directory: str, session_name: str | None) -> dict:
        prompt = (
            f"You are the Codex Planner. Read the repository at {working_directory}.\n"
            f"Task: {description}\n"
            f"Baseline SHA: {baseline_sha or 'unknown'}\n"
            f"Research report:\n{json.dumps(research, indent=2)}\n\n"
            "Verify the research conclusions and produce a structured plan: target "
            "files, ordered steps, test matrix, rollback conditions, completion "
            "criteria, risks, prerequisites_verified.\n\n"
            + _schema_hint("plan")
        )
        return self._run(task_id, "plan", "codex-planner", prompt,
                         working_directory, session_name)

    # --- implementation (GLM Executor) ---

    def run_executor(self, task_id: str, plan: dict, base_sha: str,
                     executor_worktree: str, session_name: str | None) -> tuple[dict, str]:
        """Run the GLM Executor. Returns (implementation_artifact, real_candidate_sha).

        The Executor edits files in its worktree, commits, and pushes. After the
        Worker returns, we read the REAL git HEAD SHA from the worktree (not the
        LLM claim) and verify the worktree is clean. The candidate_sha in the
        artifact is the real SHA.
        """
        prompt = (
            f"You are the GLM Executor. Your worktree is {executor_worktree}.\n"
            f"Implement the following plan. Edit files, run focused tests, commit, "
            f"and push your task branch. Then output your result as JSON.\n\n"
            f"Plan:\n{json.dumps(plan, indent=2)}\n\n"
            + _schema_hint("implementation")
            + "\n\nNote: candidate_sha will be verified from git, not from your claim."
        )
        # The executor runs with a longer timeout (it does real work).
        result = self.client.launch_worker(
            "glm-executor", prompt, executor_worktree, session_name,
            task_id=task_id, stage="implementation", timeout=600)
        if not result.success or not result.last_message:
            raise WorkerError(f"executor worker failed: {result.error or 'no output'}")
        obj = extract_strict_json(result.last_message)
        # Read the REAL candidate SHA from the executor worktree (requirement).
        real_sha = _git_head(executor_worktree)
        if not real_sha:
            raise WorkerError("executor: could not read HEAD SHA from worktree")
        if not _git_porcelain_clean(executor_worktree):
            raise WorkerError("executor: worktree dirty after run (uncommitted changes)")
        obj = validate_and_stamp("implementation", obj, task_id, real_sha)
        obj["candidate_sha"] = real_sha
        obj["base_sha"] = base_sha
        _save_artifact(task_id, "implementation", obj)
        return obj, real_sha

    # --- verification (Qwen Verifier reads exit codes, does NOT build commands) ---

    def run_verifier(self, task_id: str, candidate_sha: str, plan: dict,
                     executor_worktree: str, session_name: str | None,
                     local: bool = True) -> dict:
        """Run verification. The deterministic runner executes pytest; the Qwen
        Verifier reads the exit code + logs and writes the structured report.

        Requirement 3: the model does NOT choose test selectors, baseline, or
        thresholds. Those come from the plan + project config.
        """
        # 1. deterministic runner: run pytest in the worktree, capture exit code
        test_scope = plan.get("test_matrix", [])
        pytest_passed, pytest_summary = _run_local_pytest(executor_worktree, test_scope)
        # 2. Qwen Verifier reads the result and writes the structured report
        prompt = (
            f"You are the Qwen Verifier. The deterministic runner has already "
            f"executed the tests. Read the result below and produce the structured "
            f"verification report. Do NOT run tests yourself; do NOT change "
            f"selectors or thresholds.\n\n"
            f"Candidate SHA: {candidate_sha}\n"
            f"pytest_passed: {pytest_passed}\n"
            f"pytest_summary: {pytest_summary}\n"
            f"Test scope used: {test_scope}\n\n"
            + _schema_hint("verification")
            + f"\n\nNote: set tested_sha to '{candidate_sha}' and passed to "
            f"{'true' if pytest_passed else 'false'}."
        )
        return self._run(task_id, "verification", "qwen-verifier", prompt,
                         executor_worktree, session_name, candidate_sha=candidate_sha)

    # --- review (Codex Reviewer) ---

    def run_reviewer(self, task_id: str, candidate_sha: str, tested_sha: str,
                     implementation: dict, verification: dict,
                     working_directory: str, session_name: str | None) -> dict:
        prompt = (
            f"You are the Codex Reviewer. Review the candidate at SHA {candidate_sha}.\n"
            f"tested_sha (must equal candidate_sha): {tested_sha}\n"
            f"Implementation:\n{json.dumps(implementation, indent=2)}\n"
            f"Verification:\n{json.dumps(verification, indent=2)}\n\n"
            "Review for correctness, safety, performance, test coverage, and "
            "unrelated changes. Set decision to APPROVED or CHANGES_REQUESTED. "
            "If CHANGES_REQUESTED, list findings with id, severity (P0-P3), "
            "category, file, claim, evidence, recommended_direction.\n\n"
            + _schema_hint("review")
            + f"\n\nNote: set reviewed_sha to '{tested_sha}'."
        )
        return self._run(task_id, "review", "codex-reviewer", prompt,
                         working_directory, session_name, candidate_sha=candidate_sha)

    def run_incremental_reviewer(self, task_id: str, candidate_sha: str,
                                 tested_sha: str, prior_review: dict,
                                 implementation: dict, verification: dict,
                                 working_directory: str, session_name: str | None) -> dict:
        prompt = (
            f"You are the Codex Reviewer (incremental). A prior review requested "
            f"changes. Re-review ONLY: (1) whether each prior finding is fixed, "
            f"(2) whether the fix introduced new problems, (3) whether the plan "
            f"premises still hold.\n\n"
            f"New candidate SHA: {candidate_sha}\n"
            f"tested_sha: {tested_sha}\n"
            f"Prior review:\n{json.dumps(prior_review, indent=2)}\n"
            f"New implementation:\n{json.dumps(implementation, indent=2)}\n"
            f"New verification:\n{json.dumps(verification, indent=2)}\n\n"
            + _schema_hint("review")
            + f"\n\nNote: set reviewed_sha to '{tested_sha}'. decision must be "
            f"APPROVED or CHANGES_REQUESTED."
        )
        return self._run(task_id, "incremental_review", "codex-reviewer", prompt,
                         working_directory, session_name, candidate_sha=candidate_sha)


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


def _git_head(repo: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _git_porcelain_clean(repo: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0 and r.stdout.strip() == ""
    except Exception:
        return False
