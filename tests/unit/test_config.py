"""Unit tests for project config loader."""
from pathlib import Path

import pytest

from supervisor_cao.projects.config import load_project, list_known_projects, _deep_merge


def test_deep_merge():
    base = {"a": 1, "b": {"x": 1, "y": 2}, "c": 3}
    over = {"b": {"y": 20, "z": 30}, "d": 4}
    out = _deep_merge(base, over)
    assert out == {"a": 1, "b": {"x": 1, "y": 20, "z": 30}, "c": 3, "d": 4}


def test_load_demo_project_example():
    cfg = load_project("demo-project")
    assert cfg.name == "demo-project"
    assert cfg.base_branch == "main"
    assert cfg.task_branch_prefix == "agent/"
    assert "local" in cfg.default_verification
    assert cfg.executor_limits.get("max_rounds") == 8
    assert cfg.codex_budget.get("max_calls_per_task") == 4


def test_task_branch_format():
    cfg = load_project("demo-project")
    assert cfg.task_branch_for("T1") == "agent/T1"


def test_list_known_projects_includes_demo():
    projects = list_known_projects()
    assert "demo-project" in projects


def test_local_override_merges(tmp_path, monkeypatch):
    # point LOCAL_CONFIG_DIR to a tmp dir with an override
    import supervisor_cao.projects.config as cfgmod
    fake_local = tmp_path / "projects"
    fake_local.mkdir()
    (fake_local / "demo-project.local.yaml").write_text(
        "name: demo-project\nbase_branch: main\ncodex_budget:\n  max_calls_per_task: 2\n"
    )
    monkeypatch.setattr(cfgmod, "LOCAL_CONFIG_DIR", fake_local)
    cfg = cfgmod.load_project("demo-project")
    assert cfg.codex_budget.get("max_calls_per_task") == 2
