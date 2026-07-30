"""Generic project adapter and validation backend interfaces (spec §13).

The platform core never hard-codes a project name, base branch, test runner, or
model id. Instead it reads everything from a ``ProjectConfig`` and delegates
project-specific work to a ``ProjectAdapter`` and a ``ValidationBackend``.

- ``ProjectAdapter``: base branch, task-branch template, worktree root — all from
  project config. The core asks the adapter for these; it never assumes them.
- ``ValidationBackend``: runs local and remote verification via configured
  commands/plugins. The core only reads the exit code, logs, SHA, and structured
  result. The backend CANNOT change pass/fail — that is the exit code's job.

``local_fixture`` is a test-only marker: a backend flagged as a local fixture
MAY simulate verification in tests, but production code must never write a
simulated result into ``REMOTE_VERIFIED`` (spec: production cannot fake remote
verification).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from supervisor_cao.projects.config import ProjectConfig


class ProjectAdapter:
    """Generic adapter wrapping a ProjectConfig. The core uses this, not raw
    project names or hard-coded paths."""

    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def base_branch(self) -> str:
        return self.cfg.base_branch

    @property
    def task_branch_prefix(self) -> str:
        return self.cfg.task_branch_prefix

    def task_branch_for(self, task_id: str) -> str:
        return f"{self.task_branch_prefix}{task_id}"

    @property
    def worktree_root(self) -> Path:
        """Root directory under which per-task worktrees are created."""
        from supervisor_cao.workers.worktrees import _worktree_root
        return _worktree_root() / self.cfg.name

    @property
    def main_repo(self) -> str:
        return self.cfg.wsl_repo

    @property
    def windows_repo(self) -> str:
        return self.cfg.windows_repo

    def executor_worktree(self, task_id: str) -> Path:
        """Return the executor worktree path for a task (may not exist yet)."""
        from supervisor_cao.workers.worktrees import paths_for
        return paths_for(self.cfg.name, task_id).executor


@dataclass
class ValidationResult:
    """Structured result of one verification run. Core reads only this.

    ``passed`` is derived from ``exit_code == 0`` and is authoritative. The LLM
    verifier only contributes ``summary`` text — it can never flip ``passed``.
    """
    passed: bool
    exit_code: int
    summary: str
    tested_sha: str
    logs: dict = field(default_factory=dict)
    remote: bool = False
    local_fixture: bool = False  # True only in tests (simulated)


class ValidationBackend:
    """Generic validation backend. Runs configured commands; the core reads the
    exit code as authoritative. The model only summarizes, never decides.

    The remote path delegates to ``scripts/run-verification`` with configurable
    setup/verify commands, environment variables, container selection, and the
    task id. It reads the real ``verification.json`` the script writes.
    """

    def __init__(self, cfg: ProjectConfig, *, local_fixture: bool = False):
        self.cfg = cfg
        # local_fixture is ONLY set in tests. Production backends must never
        # mark themselves as fixtures.
        self.local_fixture = local_fixture

    # --- local ---

    def run_local(self, worktree: str, candidate_sha: str,
                  test_scope: list[str]) -> ValidationResult:
        """Run the project's configured local verification command.

        The command comes from ``default_verification.local.command``. If no
        command is configured, this returns a FAILED result with a config error
        — it does NOT default to running pytest (the platform must not assume a
        specific test runner). The exit code is authoritative:
        ``passed`` is ``exit_code == 0``.
        """
        local_cfg = self.cfg.default_verification.get("local", {})
        cmd = local_cfg.get("command")
        if not cmd:
            # No verification command configured: this is a configuration error,
            # not a soft pass. Production must configure a verification command.
            return ValidationResult(
                False, 2,
                "no local verification command configured "
                "(default_verification.local.command); refusing to default to pytest",
                candidate_sha, local_fixture=self.local_fixture)
        full = cmd if isinstance(cmd, list) else ["bash", "-lc", str(cmd)]
        try:
            r = subprocess.run(full, cwd=worktree, capture_output=True,
                               text=True, timeout=600)
            passed = r.returncode == 0
            summary = (r.stdout + r.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            return ValidationResult(False, 124, "verification timed out after 600s",
                                    candidate_sha, local_fixture=self.local_fixture)
        except FileNotFoundError:
            # runner not installed — soft pass only for local fixtures (tests)
            return ValidationResult(self.local_fixture, 0,
                                    "runner not available; skipped (local fixture)" if self.local_fixture
                                    else "runner not available; FAILED",
                                    candidate_sha, local_fixture=self.local_fixture)
        return ValidationResult(passed, r.returncode, summary, candidate_sha,
                                {"stdout": r.stdout[-500:], "stderr": r.stderr[-500:]},
                                local_fixture=self.local_fixture)

    # --- remote ---

    def run_remote(self, task_id: str, candidate_sha: str,
                   run_dir: Path) -> ValidationResult:
        """Run remote verification.

        Production: delegates to the configured remote pool via
        ``scripts/run-verification`` and reads the real exit code + the
        ``verification.json`` the script writes.

        Local-fixture (test) backends may simulate; production MUST NOT. If no
        remote pool is configured and this is not a local fixture, this returns
        a FAILED result so the state machine does NOT advance to REMOTE_VERIFIED.
        """
        if self.local_fixture:
            return ValidationResult(True, 0, "remote simulated (local fixture)",
                                     candidate_sha, remote=True, local_fixture=True)
        rv = self.cfg.remote_validation
        if not rv or not rv.get("ssh_host"):
            # No remote pool configured: production code MUST NOT fake this.
            return ValidationResult(False, 1,
                                    "no remote pool configured (production cannot fake remote verification)",
                                    candidate_sha, remote=True)
        return self._run_remote_script(task_id, candidate_sha, run_dir, rv)

    def _run_remote_script(self, task_id: str, candidate_sha: str,
                           run_dir: Path, rv: dict) -> ValidationResult:
        """Delegate to scripts/run-verification with configurable commands."""
        script = Path(__file__).resolve().parents[3] / "scripts" / "run-verification"
        containers = rv.get("containers", [])
        # container selection: prefer the first configured container
        container = containers[0] if containers else ""
        cmd = ["python", str(script),
               "--ssh-host", rv.get("ssh_host", ""),
               "--containers", ",".join(containers),
               "--user", rv.get("user", ""),
               "--repo-path", rv.get("repo_path", ""),
               "--candidate-sha", candidate_sha,
               "--task-id", task_id,
               "--run-dir", str(run_dir)]
        # configurable setup/verify commands from project config
        remote_cfg = self.cfg.default_verification.get("remote", {})
        for setup_cmd in remote_cfg.get("setup_commands", []) or []:
            cmd += ["--setup-command", setup_cmd]
        for verify_cmd in remote_cfg.get("verify_commands", []) or []:
            cmd += ["--verify-command", verify_cmd]
        if remote_cfg.get("verify_script"):
            cmd += ["--verify-script", str(remote_cfg["verify_script"])]
        for env_kv in remote_cfg.get("env", []) or []:
            cmd += ["--env", env_kv]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            passed = r.returncode == 0
            summary = (r.stdout + r.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            return ValidationResult(False, 124, "remote verification timed out",
                                    candidate_sha, remote=True)
        # read the real verification.json the script wrote
        logs = {"stdout": r.stdout[-500:] if r.stdout else "",
                "stderr": r.stderr[-500:] if r.stderr else ""}
        vjson = run_dir / "verification.json"
        if vjson.exists():
            try:
                logs["verification_json"] = json.loads(vjson.read_text())
            except Exception:
                pass
        return ValidationResult(passed, r.returncode, summary, candidate_sha,
                                logs, remote=True)

    # --- artifact writing ---

    def write_artifact(self, result: ValidationResult, run_dir: Path,
                       *, remote: bool = False, task_id: str = "") -> Path:
        """Write/merge the verification.json artifact. The authoritative fields
        (passed, tested_sha, exit_code) come from the result, never from the
        LLM. An optional LLM summary may be merged under ``llm_summary``.

        After writing, the artifact is validated against the verification JSON
        schema. If validation fails, the artifact is still written (so evidence
        is preserved) but a warning is printed to stderr.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "verification.json"
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text()) or {}
            except Exception:
                existing = {}
        # authoritative fields from the backend (never overwritten by LLM)
        if task_id:
            existing["task_id"] = task_id
        existing["passed"] = result.passed
        existing["tested_sha"] = result.tested_sha
        existing["candidate_sha"] = result.tested_sha
        # exit_code is authoritative evidence; store under logs (the schema
        # declares logs as an object with additionalProperties:true).
        existing.setdefault("logs", {})["exit_code"] = result.exit_code
        if remote:
            existing["remote_results"] = {
                "container": "configured",
                "install_ok": result.passed,
                "correctness_passed": result.passed,
                "summary": result.summary,
            }
        else:
            existing["wsl_results"] = {
                "build": True,
                "pytest_passed": result.passed,
                "summary": result.summary,
            }
        existing.setdefault("environment", {})
        path.write_text(json.dumps(existing, indent=2))
        # validate against the verification schema immediately after writing
        _validate_verification_artifact(existing)
        return path


def _validate_verification_artifact(obj: dict) -> None:
    """Validate the verification artifact against schemas/verification.schema.json.

    Best-effort: if jsonschema is unavailable or validation fails, prints a
    warning but does not raise (the artifact is evidence and must be preserved).
    """
    try:
        import jsonschema
        schema_path = Path(__file__).resolve().parents[3] / "schemas" / "verification.schema.json"
        schema = json.loads(schema_path.read_text())
        jsonschema.validate(obj, schema)
    except Exception as e:
        import sys
        print(f"WARNING: verification artifact schema validation failed: {e}",
              file=sys.stderr)
