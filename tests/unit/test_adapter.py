"""Unit tests for the generic ProjectAdapter and ValidationBackend (spec §13)."""
import subprocess

import pytest

from supervisor_cao.projects.config import ProjectConfig
from supervisor_cao.projects.adapter import ProjectAdapter, ValidationBackend, ValidationResult


def _cfg(**kw):
    base = dict(name="demo-project", base_branch="main", task_branch_prefix="agent/")
    base.update(kw)
    return ProjectConfig(**base)


def test_adapter_base_branch_from_config():
    cfg = _cfg(base_branch="trunk")
    a = ProjectAdapter(cfg)
    assert a.base_branch == "trunk"
    assert a.name == "demo-project"


def test_adapter_task_branch_from_config():
    cfg = _cfg(task_branch_prefix="feat/")
    a = ProjectAdapter(cfg)
    assert a.task_branch_for("T1") == "feat/T1"


def test_adapter_worktree_root_uses_project_name():
    cfg = _cfg()
    a = ProjectAdapter(cfg)
    assert a.worktree_root.name == "demo-project"


def test_validation_result_pass_is_exit_code_zero():
    r = ValidationResult(passed=True, exit_code=0, summary="ok", tested_sha="c1", logs={})
    assert r.passed is True
    assert r.local_fixture is False


def test_validation_backend_local_fixture_default_false():
    cfg = _cfg()
    b = ValidationBackend(cfg)
    assert b.local_fixture is False


def test_validation_backend_local_fixture_only_in_tests():
    cfg = _cfg()
    b = ValidationBackend(cfg, local_fixture=True)
    assert b.local_fixture is True


def test_validation_backend_run_remote_production_cannot_fake(tmp_path):
    """Production backend with no remote pool configured MUST NOT fake REMOTE_VERIFIED."""
    cfg = _cfg()  # no remote_validation configured
    b = ValidationBackend(cfg, local_fixture=False)
    r = b.run_remote("c1", tmp_path)
    assert r.passed is False
    assert r.remote is True
    assert r.local_fixture is False


def test_validation_backend_run_remote_local_fixture_simulates(tmp_path):
    """A local-fixture (test) backend may simulate remote verification."""
    cfg = _cfg()
    b = ValidationBackend(cfg, local_fixture=True)
    r = b.run_remote("c1", tmp_path)
    assert r.passed is True
    assert r.local_fixture is True


def test_validation_backend_run_local_reads_exit_code(tmp_path):
    """run_local uses the configured command's exit code as authoritative."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    cfg = _cfg(default_verification={"local": {"command": ["true"]}})
    b = ValidationBackend(cfg, local_fixture=False)
    r = b.run_local(str(worktree), "c1", [])
    assert r.passed is True
    assert r.exit_code == 0
    assert r.local_fixture is False


def test_validation_backend_run_local_failure_exit_code(tmp_path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    cfg = _cfg(default_verification={"local": {"command": ["false"]}})
    b = ValidationBackend(cfg, local_fixture=False)
    r = b.run_local(str(workdir), "c1", [])
    assert r.passed is False
    assert r.exit_code != 0
