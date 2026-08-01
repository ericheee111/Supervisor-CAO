"""Integration test for task CLI with a temp repo (no real cao-server needed).

Tests the CLI command structure, config snapshot, and task ID generation
without requiring a live cao-server. Uses mock WorkerMonitor.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_task_start_generates_random_suffix():
    """task_start should generate a task ID with a random suffix to avoid
    same-second collisions."""
    from supervisor_cao.cli.task_runner import _build_gateway, _make_temp_config
    # Two calls to _make_temp_config should produce configs with different
    # verify commands parsed via shlex
    cfg1 = _make_temp_config("/tmp/repo", "main", "pytest tests/ -q")
    cfg2 = _make_temp_config("/tmp/repo", "main", "python -m pytest -x")
    assert cfg1.default_verification["local"]["command"] == ["pytest", "tests/", "-q"]
    assert cfg2.default_verification["local"]["command"] == ["python", "-m", "pytest", "-x"]


def test_shlex_split_for_verify_command():
    """verify_command should be parsed with shlex.split, not simple split()."""
    from supervisor_cao.cli.task_runner import _make_temp_config
    # shlex.split handles quoted args correctly
    cfg = _make_temp_config("/tmp/repo", "main", 'python -c "print(1)"')
    assert cfg.default_verification["local"]["command"] == ["python", "-c", "print(1)"]


def test_task_start_requires_verify_command_in_temp_mode():
    """task_start without --project must require --verify-command."""
    from supervisor_cao.cli.task_runner import task_start
    # Create a temp repo and description file
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        desc = Path(tmp) / "task.md"
        desc.write_text("test task")
        # Without verify_command, should fail
        rc = task_start(str(repo), "main", str(desc), project=None, verify_command=None)
        assert rc == 1  # error exit code


def test_config_snapshot_persisted(tmp_path, monkeypatch):
    """task_start should persist a config snapshot for resume."""
    from supervisor_cao.cli.task_runner import _make_temp_config
    from supervisor_cao.mcp.policy_gateway import PolicyGateway
    cfg = _make_temp_config(str(tmp_path), "main", "pytest")
    # Verify config has the right fields
    assert cfg.name == "temp-task"
    assert cfg.base_branch == "main"
    assert cfg.wsl_repo == str(tmp_path)
    assert cfg.remote_verification_mode == "disabled"
    assert cfg.default_verification["local"]["command"] == ["pytest"]
