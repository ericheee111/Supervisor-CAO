"""Unit tests for config safety: task override filtering (spec §6)."""
import pytest

from supervisor_cao.projects.config import _filter_task_override, ALLOWED_TASK_OVERRIDE_KEYS, FORBIDDEN_TASK_OVERRIDE_KEYS


def test_allowed_override_keys_pass():
    raw = {"baseline_sha": "abc123", "benchmark_selector": "groupby.time_loop",
           "required_test_scope": ["test_a"]}
    filtered = _filter_task_override(raw)
    assert filtered == raw


def test_forbidden_override_rejected():
    raw = {"wsl_repo": "/tmp/evil", "base_branch": "master"}
    with pytest.raises(ValueError, match="project-level field"):
        _filter_task_override(raw)


def test_remote_validation_in_override_rejected():
    raw = {"remote_validation": {"ssh_host": "evil-host"}}
    with pytest.raises(ValueError, match="project-level field"):
        _filter_task_override(raw)


def test_container_in_override_rejected():
    raw = {"remote_validation": {"containers": ["evil-container"]}}
    with pytest.raises(ValueError, match="project-level field"):
        _filter_task_override(raw)


def test_unknown_keys_silently_ignored():
    raw = {"baseline_sha": "abc", "unknown_future_key": "value"}
    filtered = _filter_task_override(raw)
    assert "baseline_sha" in filtered
    assert "unknown_future_key" not in filtered


def test_name_is_allowed():
    raw = {"name": "pandas"}
    filtered = _filter_task_override(raw)
    assert filtered["name"] == "pandas"


def test_executor_limits_not_overridable():
    raw = {"executor_limits": {"max_rounds": 999}}
    with pytest.raises(ValueError, match="project-level field"):
        _filter_task_override(raw)


def test_codex_budget_not_overridable():
    raw = {"codex_budget": {"max_calls_per_task": 100}}
    with pytest.raises(ValueError, match="project-level field"):
        _filter_task_override(raw)
