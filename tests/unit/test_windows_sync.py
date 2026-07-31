"""Unit tests for Windows sync gates (spec §17, §20.1)."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from supervisor_cao.validation.windows_sync import (
    check_gates, sync, WindowsSyncBlocked, _git_porcelain_clean,
    validate_pr_content_artifact,
)


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


def _write_pr_content(run_dir, task_id="T1", candidate="c1",
                     head_branch="agent/T1", workflow_state="PR_CONTENT_READY"):
    run_dir.mkdir(parents=True, exist_ok=True)
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {
        "plan": {"steps": [{"description": "x"}]},
        "implementation": {"candidate_sha": candidate, "changed_files": ["a.py"]},
        "verification": {"candidate_sha": candidate, "tested_sha": candidate,
                         "wsl_results": {}, "remote_results": {}},
        "review": {"reviewed_sha": candidate, "decision": "APPROVED", "findings": []},
        "budget": {"total_used": 1, "remaining": 3},
    }
    push = {"schema_version": 1, "remote": "origin", "branch": head_branch,
            "pushed_sha": candidate, "push_succeeded": True}
    j, m, s = render_pr_content(arts, task_id, "main", head_branch, push)
    if workflow_state != "PR_CONTENT_READY":
        # tamper workflow_state for testing
        j_obj = json.loads(j)
        j_obj["workflow_state"] = workflow_state
        j = json.dumps(j_obj, indent=2, ensure_ascii=False) + "\n"
    (run_dir / "pr-content.json").write_text(j)
    (run_dir / "pr-content.md").write_text(m)
    (run_dir / "pr-content.sha256").write_text(s)
    (run_dir / "push.json").write_text(json.dumps(push))


# --- validate_pr_content_artifact ---

def test_validate_pr_content_artifact_pass(tmp_path):
    _write_pr_content(tmp_path, candidate="c1", head_branch="agent/T1")
    assert validate_pr_content_artifact(tmp_path, "T1", "c1", "c1", "c1",
                                        "main", "agent/T1") is True


def test_validate_pr_content_artifact_missing_files(tmp_path):
    assert validate_pr_content_artifact(tmp_path, "T1", "c1", "c1", "c1",
                                        "main", "agent/T1") is False


def test_validate_pr_content_artifact_sha256_tampered(tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    (tmp_path / "pr-content.json").write_text('{"tampered": true}\n')
    assert validate_pr_content_artifact(tmp_path, "T1", "c1", "c1", "c1",
                                        "main", "agent/T1") is False


def test_validate_pr_content_artifact_wrong_workflow_state(tmp_path):
    _write_pr_content(tmp_path, candidate="c1",
                     workflow_state="READY_FOR_HUMAN_REVIEW")
    assert validate_pr_content_artifact(tmp_path, "T1", "c1", "c1", "c1",
                                        "main", "agent/T1") is False


def test_validate_pr_content_artifact_wrong_task_id(tmp_path):
    _write_pr_content(tmp_path, task_id="T1")
    assert validate_pr_content_artifact(tmp_path, "WRONG", "c1", "c1", "c1",
                                        "main", "agent/T1") is False


def test_validate_pr_content_artifact_wrong_sha(tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    assert validate_pr_content_artifact(tmp_path, "T1", "WRONG", "c1", "c1",
                                        "main", "agent/T1") is False


def test_validate_pr_content_artifact_push_missing(tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    (tmp_path / "push.json").unlink()
    assert validate_pr_content_artifact(tmp_path, "T1", "c1", "c1", "c1",
                                        "main", "agent/T1") is False


def test_validate_pr_content_artifact_push_sha_mismatch(tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    push = json.loads((tmp_path / "push.json").read_text())
    push["pushed_sha"] = "WRONG"
    (tmp_path / "push.json").write_text(json.dumps(push))
    assert validate_pr_content_artifact(tmp_path, "T1", "c1", "c1", "c1",
                                        "main", "agent/T1") is False


# --- check_gates ---

def test_gates_fail_when_windows_dirty(fake_repo, tmp_path):
    Path(fake_repo, "new.txt").write_text("dirty")
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.windows_clean is False
    assert gates.all_pass is False


def test_gates_fail_when_tested_neq_candidate(fake_repo, tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    gates = check_gates(fake_repo, "agent/T1", "c1", "WRONG", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.tested_eq_candidate is False
    assert gates.all_pass is False


def test_gates_fail_when_not_approved(fake_repo, tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=False, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.review_approved is False
    assert gates.all_pass is False


def test_gates_fail_when_pr_content_missing(fake_repo, tmp_path):
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is False
    assert gates.all_pass is False


def test_gates_pass_when_pr_content_valid(fake_repo, tmp_path):
    _write_pr_content(tmp_path, candidate="c1", head_branch="agent/T1")
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is True


def test_gates_fail_when_sha256_tampered(fake_repo, tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    (tmp_path / "pr-content.json").write_text('{"tampered": true}\n')
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is False


# --- sync ---

def test_sync_blocked_on_dirty(fake_repo, tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    Path(fake_repo, "new.txt").write_text("dirty")
    with pytest.raises(WindowsSyncBlocked) as ei:
        sync(fake_repo, "agent/T1", "c1", "c1", "c1",
             review_approved=True, run_dir=tmp_path, task_id="T1",
             base_branch="main", head_branch="agent/T1")
    assert "WINDOWS_SYNC_BLOCKED" in str(ei.value)


def test_sync_blocked_on_sha_mismatch(fake_repo, tmp_path):
    _write_pr_content(tmp_path, candidate="c1")
    with pytest.raises(WindowsSyncBlocked):
        sync(fake_repo, "agent/T1", "c1", "WRONG", "c1",
             review_approved=True, run_dir=tmp_path, task_id="T1",
             base_branch="main", head_branch="agent/T1")
