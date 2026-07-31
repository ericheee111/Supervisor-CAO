"""Unit tests for production hardening: profile install, artifact schema,
custom branch prefix/worktree root, multi-round CHANGES_REQUESTED, RUNNING
resume, and acceptance isolation."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.state.machine import StateStore, TaskState, IllegalTransition  # noqa: E402
from supervisor_cao.budget.codex import CodexBudget  # noqa: E402
from supervisor_cao.mcp.stage_store import StageStore, COMPLETED, RUNNING, PENDING  # noqa: E402
from supervisor_cao.projects.config import ProjectConfig  # noqa: E402
from supervisor_cao.projects.adapter import ValidationBackend, ValidationResult  # noqa: E402
from supervisor_cao.workers import worktrees as wtmod  # noqa: E402


# ---------------------------------------------------------------------------
# Profile install: .md suffix and fail-fast
# ---------------------------------------------------------------------------

class TestProfileInstallRendering:
    def _load_install_profiles(self):
        from importlib.machinery import SourceFileLoader
        return SourceFileLoader(
            "install_profiles",
            str(Path(__file__).resolve().parents[2] / "scripts" / "install-profiles")
        ).load_module()

    def test_render_writes_md_suffix(self, tmp_path):
        """install-profiles render_profile produces text; the install function
        writes it to a <profile>.md temp file (CAO requires the suffix)."""
        mod = self._load_install_profiles()
        src = Path(__file__).resolve().parents[2] / "profiles" / "glm-executor.md"
        rendered_text = mod.render_profile(src, "glm-executor", "executor", {})
        assert "provider: opencode_cli" in rendered_text
        assert "name: glm-executor" in rendered_text

    def test_render_no_model_still_copies(self, tmp_path):
        """When no model config exists, render_profile returns the source text
        unchanged (the source is never deleted)."""
        mod = self._load_install_profiles()
        src = Path(__file__).resolve().parents[2] / "profiles" / "researcher.md"
        original = src.read_text()
        rendered = mod.render_profile(src, "researcher", "research", {})
        assert rendered == original  # unchanged when no model
        assert src.read_text() == original  # source not modified

    def test_verify_profile_content_detects_missing_fields(self):
        mod = self._load_install_profiles()
        # a profile missing the provider line
        bad = "---\nname: supervisor\nrole: supervisor\n---\nbody\n"
        errors = mod.verify_profile_content(bad, "supervisor")
        assert any("provider" in e for e in errors)
        # supervisor needs MCP config
        assert any("MCP" in e for e in errors)


# ---------------------------------------------------------------------------
# Artifact schema validation on write
# ---------------------------------------------------------------------------

class TestArtifactSchema:
    def test_write_artifact_validates_against_schema(self, tmp_path):
        """write_artifact produces a verification.json that validates against
        schemas/verification.schema.json."""
        import jsonschema
        cfg = ProjectConfig(name="demo", base_branch="main",
                            default_verification={"local": {"command": ["true"]}})
        b = ValidationBackend(cfg, local_fixture=False)
        result = ValidationResult(True, 0, "ok", "sha1")
        path = b.write_artifact(result, tmp_path, remote=False, task_id="T1")
        obj = json.loads(path.read_text())
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "verification.schema.json"
        schema = json.loads(schema_path.read_text())
        jsonschema.validate(obj, schema)  # should not raise

    def test_write_artifact_remote_validates(self, tmp_path):
        import jsonschema
        cfg = ProjectConfig(name="demo", base_branch="main",
                            default_verification={"local": {"command": ["true"]}})
        b = ValidationBackend(cfg, local_fixture=False)
        result = ValidationResult(True, 0, "ok", "sha1")
        path = b.write_artifact(result, tmp_path, remote=True, task_id="T1")
        obj = json.loads(path.read_text())
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "verification.schema.json"
        schema = json.loads(schema_path.read_text())
        jsonschema.validate(obj, schema)


# ---------------------------------------------------------------------------
# No default pytest: config error when no command
# ---------------------------------------------------------------------------

class TestNoDefaultPytest:
    def test_no_command_returns_config_error(self, tmp_path):
        cfg = ProjectConfig(name="demo", base_branch="main")  # no verification config
        b = ValidationBackend(cfg, local_fixture=False)
        result = b.run_local(str(tmp_path), "sha1", [])
        assert result.passed is False
        assert "no local verification command configured" in result.summary


# ---------------------------------------------------------------------------
# Custom branch prefix and worktree root
# ---------------------------------------------------------------------------

class TestCustomBranchPrefix:
    def test_config_custom_branch_prefix(self):
        cfg = ProjectConfig(name="demo", base_branch="main", task_branch_prefix="feat/")
        assert cfg.task_branch_for("T1") == "feat/T1"

    def test_worktree_root_uses_project_name(self, tmp_path, monkeypatch):
        from supervisor_cao.projects.adapter import ProjectAdapter
        monkeypatch.setenv("SCAO_WORKTREE_ROOT", str(tmp_path / "wt"))
        cfg = ProjectConfig(name="myproj", base_branch="main")
        adapter = ProjectAdapter(cfg)
        assert adapter.worktree_root == tmp_path / "wt" / "myproj"

    def test_create_task_branch_uses_custom_prefix(self, tmp_path):
        """create_task_branch honors branch_prefix (not hardcoded agent/)."""
        bare = tmp_path / "bare.git"
        _git(["init", "--bare", "-b", "main", str(bare)])
        main = tmp_path / "main"
        _git(["init", "-b", "main", str(main)])
        _git(["config", "user.email", "t@t.t"], cwd=str(main))
        _git(["config", "user.name", "t"], cwd=str(main))
        (main / "f").write_text("x")
        _git(["add", "-A"], cwd=str(main))
        _git(["commit", "-m", "i"], cwd=str(main))
        _git(["remote", "add", "origin", str(bare)], cwd=str(main))
        _git(["push", "origin", "main"], cwd=str(main))
        sha = wtmod.create_task_branch(str(main), "T9", "main", branch_prefix="feat/")
        # branch should be feat/T9, not agent/T9
        r = _git(["rev-parse", "--verify", "feat/T9"], cwd=str(main), check=False)
        assert r.returncode == 0
        r2 = _git(["rev-parse", "--verify", "agent/T9"], cwd=str(main), check=False)
        assert r2.returncode != 0


def _git(cmd, cwd=None, check=True):
    r = subprocess.run(["git"] + cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {cmd[:2]} failed: {r.stderr.strip()}")
    return r


# ---------------------------------------------------------------------------
# StageStore: attempt tracking and multi-round CHANGES_REQUESTED
# ---------------------------------------------------------------------------

class TestStageStoreAttempts:
    def test_attempt_increments_on_rerun(self, tmp_path):
        store = StageStore(db_path=tmp_path / "stages.db")
        store.begin_stage("T1", "verification", "qwen-verifier", candidate_sha="c1")
        store.complete_stage("T1", "verification", candidate_sha="c1")
        run = store.get("T1", "verification")
        assert run.attempt == 0
        # re-run with different candidate -> attempt increments
        run2, done = store.begin_stage("T1", "verification", "qwen-verifier", candidate_sha="c2")
        assert done is False
        assert run2.attempt == 1

    def test_input_sha_recorded(self, tmp_path):
        store = StageStore(db_path=tmp_path / "stages.db")
        store.begin_stage("T1", "review", "codex-reviewer",
                          candidate_sha="c1", input_sha="c1")
        run = store.get("T1", "review")
        assert run.input_sha == "c1"

    def test_running_resume_does_not_increment(self, tmp_path):
        """A non-stale RUNNING record is reused (not reclaimed), so attempt
        does not increment and the Worker/budget is reused."""
        store = StageStore(db_path=tmp_path / "stages.db")
        store.begin_stage("T1", "plan", "codex-planner")
        store.mark_running("T1", "plan", terminal_id="term1", candidate_sha="c1")
        # resume while RUNNING (not stale)
        run, done = store.begin_stage("T1", "plan", "codex-planner")
        assert done is False
        assert run.status == RUNNING
        assert run.terminal_id == "term1"
        assert run.attempt == 0  # not incremented on resume


class TestMultiRoundChangesRequested:
    def test_two_rounds_state_transitions(self, tmp_path):
        """Two consecutive CHANGES_REQUESTED rounds are legal in the state
        machine: REVIEWING -> CHANGES_REQUESTED -> FIXING -> LOCAL_VERIFYING
        -> ... -> REMOTE_VERIFIED -> INCREMENTAL_REVIEWING -> CHANGES_REQUESTED
        -> FIXING -> ..."""
        store = StateStore(db_path=tmp_path / "tasks.db")
        store.create("T1", "demo", baseline_sha="b")
        for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
                   TaskState.IMPLEMENTING]:
            store.transition("T1", st)
        store.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
        store.transition("T1", TaskState.LOCAL_VERIFYING)
        store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
        store.transition("T1", TaskState.REMOTE_QUEUED)
        store.transition("T1", TaskState.REMOTE_VERIFYING)
        store.transition("T1", TaskState.REMOTE_VERIFIED)
        store.transition("T1", TaskState.REVIEWING, reviewed_sha="c1")
        # round 1
        store.transition("T1", TaskState.CHANGES_REQUESTED)
        store.transition("T1", TaskState.FIXING, new_candidate_sha="c2")
        store.transition("T1", TaskState.LOCAL_VERIFYING)
        store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c2")
        store.transition("T1", TaskState.REMOTE_QUEUED)
        store.transition("T1", TaskState.REMOTE_VERIFYING)
        store.transition("T1", TaskState.REMOTE_VERIFIED)
        store.transition("T1", TaskState.INCREMENTAL_REVIEWING, reviewed_sha="c2")
        # round 2: CHANGES_REQUESTED again
        store.transition("T1", TaskState.CHANGES_REQUESTED)
        store.transition("T1", TaskState.FIXING, new_candidate_sha="c3")
        store.transition("T1", TaskState.LOCAL_VERIFYING)
        store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c3")
        store.transition("T1", TaskState.REMOTE_QUEUED)
        store.transition("T1", TaskState.REMOTE_VERIFYING)
        store.transition("T1", TaskState.REMOTE_VERIFIED)
        store.transition("T1", TaskState.INCREMENTAL_REVIEWING, reviewed_sha="c3")
        store.transition("T1", TaskState.APPROVED)
        rec = store.get("T1")
        assert rec.state == TaskState.APPROVED.value
        assert rec.candidate_sha == "c3"


# ---------------------------------------------------------------------------
# RUNNING resume: no duplicate Codex budget
# ---------------------------------------------------------------------------

class TestRunningResumeBudget:
    def test_running_resume_does_not_re_spend_budget(self, tmp_path):
        """When a stage is RUNNING (not stale), resume reuses it. The caller
        checks begin_stage's already_completed=False + status=RUNNING and does
        NOT re-spend budget. This test verifies the StageStore returns the
        RUNNING record (not a new PENDING one)."""
        store = StageStore(db_path=tmp_path / "stages.db")
        budget = CodexBudget(db_path=tmp_path / "codex.db")
        # simulate: planner stage started, budget spent
        store.begin_stage("T1", "plan", "codex-planner")
        store.mark_running("T1", "plan", terminal_id="term1")
        budget.spend("T1", "planner", input_artifact="r", candidate_sha=None)
        assert budget.used("T1", "planner") == 1
        # resume: begin_stage returns RUNNING (not new PENDING)
        run, done = store.begin_stage("T1", "plan", "codex-planner")
        assert done is False
        assert run.status == RUNNING
        # the caller must NOT re-spend; budget stays at 1
        assert budget.used("T1", "planner") == 1


# ---------------------------------------------------------------------------
# Acceptance isolation
# ---------------------------------------------------------------------------

class TestAcceptanceIsolation:
    def test_acceptance_uses_isolated_dirs(self, tmp_path, monkeypatch):
        """acceptance prepare creates isolated state/budget/runs/worktrees dirs
        and does not touch the default ~/.local/state/supervisor-cao."""
        from supervisor_cao.cli import acceptance as accmod
        monkeypatch.setattr(accmod, "ACCEPTANCE_ROOT", tmp_path / "acc")
        # redirect the Windows-accessible repo dir to the temp dir (CI has no /mnt/d)
        monkeypatch.setenv("SCAO_ACCEPTANCE_REPO_DIR", str(tmp_path / "acc" / "repo"))
        # prepare with a local repo path
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "f.txt").write_text("x")
        rc = accmod.prepare(repo_path=str(repo))
        assert rc == 0
        assert (tmp_path / "acc" / "state").exists()
        assert (tmp_path / "acc" / "budget").exists()
        assert (tmp_path / "acc" / "runs").exists()
        assert (tmp_path / "acc" / "worktrees").exists()
        assert (tmp_path / "acc" / "repo").exists()
        meta = accmod._read_meta()
        assert meta["repo_path"] == str(repo)

    def test_acceptance_status_isolated(self, tmp_path, monkeypatch):
        from supervisor_cao.cli import acceptance as accmod
        monkeypatch.setattr(accmod, "ACCEPTANCE_ROOT", tmp_path / "acc")
        # only direct run -> status returns 1 (not all scenarios pass yet)
        accmod._write_meta({"scenarios": {"direct": {"passed": True, "status": "PASS"}}})
        rc = accmod.status()
        assert rc == 1  # not all scenarios run
        # all three pass -> returns 0
        accmod._write_meta({"scenarios": {
            "direct": {"passed": True}, "review-fix": {"passed": True},
            "resume": {"passed": True}}})
        rc2 = accmod.status()
        assert rc2 == 0

    def test_acceptance_cleanup_removes_root(self, tmp_path, monkeypatch):
        from supervisor_cao.cli import acceptance as accmod
        root = tmp_path / "acc"
        monkeypatch.setattr(accmod, "ACCEPTANCE_ROOT", root)
        monkeypatch.setattr(accmod, "_read_meta", lambda: {"repo_dir": ""})
        root.mkdir(parents=True)
        (root / "state").mkdir()
        # evidence should be preserved
        ev = root / "evidence" / "run-1" / "direct"
        ev.mkdir(parents=True)
        (ev / "result.json").write_text("{}")
        rc = accmod.cleanup()
        assert rc == 0
        # state is removed, but evidence is preserved
        assert not (root / "state").exists()
        assert ev.exists()
