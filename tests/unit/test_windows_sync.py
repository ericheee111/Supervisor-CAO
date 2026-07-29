"""Unit tests for Windows sync gates (spec §17, §20.1)."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from supervisor_cao.validation.windows_sync import check_gates, sync, WindowsSyncBlocked, _git_porcelain_clean


@pytest.fixture
def fake_repo(tmp_path):
    """Create a real git repo to test gate logic."""
    repo = tmp_path / "winrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return str(repo)


def test_clean_repo_detected(fake_repo):
    assert _git_porcelain_clean(fake_repo) is True


def test_dirty_repo_detected(fake_repo):
    Path(fake_repo, "new.txt").write_text("dirty")
    assert _git_porcelain_clean(fake_repo) is False


def test_gates_fail_when_windows_dirty(fake_repo):
    Path(fake_repo, "new.txt").write_text("dirty")
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1", True, True)
    assert gates.windows_clean is False
    assert gates.all_pass is False


def test_gates_fail_when_tested_neq_candidate(fake_repo):
    gates = check_gates(fake_repo, "agent/T1", "c1", "WRONG", "c1", True, True)
    assert gates.tested_eq_candidate is False
    assert gates.all_pass is False


def test_gates_fail_when_not_approved(fake_repo):
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1", False, True)
    assert gates.review_approved is False
    assert gates.all_pass is False


def test_gates_fail_when_no_draft_pr(fake_repo):
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1", True, False)
    assert gates.draft_pr_created is False
    assert gates.all_pass is False


def test_sync_blocked_on_dirty(fake_repo):
    Path(fake_repo, "new.txt").write_text("dirty")
    with pytest.raises(WindowsSyncBlocked) as ei:
        sync(fake_repo, "agent/T1", "c1", "c1", "c1", True, True)
    assert "WINDOWS_SYNC_BLOCKED" in str(ei.value)


def test_sync_blocked_on_sha_mismatch(fake_repo):
    with pytest.raises(WindowsSyncBlocked):
        sync(fake_repo, "agent/T1", "c1", "WRONG", "c1", True, True)
