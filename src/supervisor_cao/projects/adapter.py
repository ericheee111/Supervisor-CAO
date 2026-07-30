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

from dataclasses import dataclass
from pathlib import Path

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
        from supervisor_cao.workers.worktrees import WORKTREE_ROOT
        return WORKTREE_ROOT / self.cfg.name

    @property
    def main_repo(self) -> str:
        return self.cfg.wsl_repo

    @property
    def windows_repo(self) -> str:
        return self.cfg.windows_repo


@dataclass
class ValidationResult:
    """Structured result of one verification run. Core reads only this."""
    passed: bool
    exit_code: int
    summary: str
    tested_sha: str
    logs: dict
    remote: bool = False
    local_fixture: bool = False  # True only in tests (simulated)


class ValidationBackend:
    """Generic validation backend. Runs configured commands; the core reads the
    exit code as authoritative. The model only summarizes, never decides."""

    def __init__(self, cfg: ProjectConfig, *, local_fixture: bool = False):
        self.cfg = cfg
        # local_fixture is ONLY set in tests. Production backends must never
        # mark themselves as fixtures.
        self.local_fixture = local_fixture

    def run_local(self, worktree: str, candidate_sha: str,
                  test_scope: list[str]) -> ValidationResult:
        """Run the project's configured local verification command.

        Default implementation runs the project's ``default_verification.local``
        command if configured, else a discovery smoke test. The exit code is
        authoritative: ``passed`` is ``exit_code == 0``.
        """
        import subprocess
        local_cfg = self.cfg.default_verification.get("local", {})
        cmd = local_cfg.get("command")
        if cmd:
            full = cmd if isinstance(cmd, list) else ["bash", "-lc", str(cmd)]
        else:
            # discovery smoke test (no project-specific runner assumed)
            targets = test_scope if test_scope else ["-q", "--no-header"]
            full = ["python", "-m", "pytest", *targets]
        try:
            r = subprocess.run(full, cwd=worktree, capture_output=True,
                               text=True, timeout=600)
            passed = r.returncode == 0
            summary = (r.stdout + r.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            return ValidationResult(False, 124, "verification timed out after 600s",
                                    candidate_sha, {}, remote=False,
                                    local_fixture=self.local_fixture)
        except FileNotFoundError:
            # runner not installed — soft pass only for local fixtures (tests)
            return ValidationResult(self.local_fixture, 0,
                                    "runner not available; skipped (local fixture)" if self.local_fixture
                                    else "runner not available; FAILED",
                                    candidate_sha, {}, remote=False,
                                    local_fixture=self.local_fixture)
        return ValidationResult(passed, r.returncode, summary, candidate_sha,
                                {"stdout": r.stdout[-500:], "stderr": r.stderr[-500:]},
                                remote=False, local_fixture=self.local_fixture)

    def run_remote(self, candidate_sha: str, run_dir: Path) -> ValidationResult:
        """Run remote verification. Production: delegates to the configured
        remote pool / scripts/run-verification and reads the real exit code.
        Local-fixture (test) backends may simulate; production MUST NOT."""
        if self.local_fixture:
            return ValidationResult(True, 0, "remote simulated (local fixture)",
                                     candidate_sha, {}, remote=True,
                                     local_fixture=True)
        rv = self.cfg.remote_validation
        if not rv or not rv.get("ssh_host"):
            # No remote pool configured: production code MUST NOT fake this.
            # Return a failure so the state machine does not advance to
            # REMOTE_VERIFIED on a simulated result.
            return ValidationResult(False, 1,
                                    "no remote pool configured (production cannot fake remote verification)",
                                    candidate_sha, {}, remote=True,
                                    local_fixture=False)
        # Real remote path: delegate to scripts/run-verification.
        import subprocess
        script = Path(__file__).resolve().parents[3] / "scripts" / "run-verification"
        cmd = ["python", str(script), "--ssh-host", rv.get("ssh_host", ""),
               "--containers", ",".join(rv.get("containers", [])),
               "--user", rv.get("user", ""), "--repo-path", rv.get("repo_path", ""),
               "--candidate-sha", candidate_sha, "--run-dir", str(run_dir)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            passed = r.returncode == 0
            summary = (r.stdout + r.stderr)[-1500:]
        except subprocess.TimeoutExpired:
            return ValidationResult(False, 124, "remote verification timed out",
                                    candidate_sha, {}, remote=True)
        return ValidationResult(passed, r.returncode, summary, candidate_sha,
                                {"stdout": r.stdout[-500:], "stderr": r.stderr[-500:]},
                                remote=True)
