"""Unit tests for append-only acceptance evidence and cleanup."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_evidence_dir_unique_per_run(tmp_path):
    from supervisor_cao.cli.acceptance import _evidence_dir
    d1 = _evidence_dir(tmp_path, "direct", "run-1")
    d2 = _evidence_dir(tmp_path, "direct", "run-2")
    assert d1 != d2
    assert "direct" in str(d1)
    assert d1.exists() and d2.exists()


def test_record_evidence_writes_files(tmp_path):
    from supervisor_cao.cli.acceptance import _evidence_dir, _record_evidence
    ev_dir = _evidence_dir(tmp_path, "direct", "run-1")
    _record_evidence(ev_dir,
                     result={"passed": True},
                     task_snapshot={"task_id": "T1", "state": "READY_FOR_HUMAN_REVIEW"},
                     events=[{"event": "CREATE"}, {"event": "TRANSITION"}],
                     stage_attempts=[{"stage": "plan", "status": "COMPLETED"}],
                     budget_log={"total_used": 1},
                     worker_handles=[{"worker_id": "w1"}],
                     sha_info={"candidate": "abc", "tested": "abc", "reviewed": "abc"},
                     pr_content_info={"valid": True})
    assert (ev_dir / "result.json").exists()
    assert (ev_dir / "task_snapshot.json").exists()
    assert (ev_dir / "events.jsonl").exists()
    assert (ev_dir / "stage_attempts.json").exists()
    assert (ev_dir / "budget_log.json").exists()
    assert (ev_dir / "worker_handles.json").exists()
    assert (ev_dir / "sha_info.json").exists()
    assert (ev_dir / "pr_content_info.json").exists()
    # events.jsonl has one JSON object per line
    lines = (ev_dir / "events.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2


def test_cleanup_preserves_evidence(tmp_path):
    """cleanup must NOT delete acceptance/evidence/."""
    from supervisor_cao.cli.acceptance import cleanup
    ev_dir = tmp_path / "acceptance" / "evidence" / "run-1" / "direct"
    ev_dir.mkdir(parents=True)
    (ev_dir / "result.json").write_text('{"passed": true}')
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        with patch("supervisor_cao.cli.acceptance._read_meta", return_value={"repo_dir": ""}):
            cleanup()
    assert ev_dir.exists(), "evidence must be preserved by cleanup"
    assert (ev_dir / "result.json").exists()


def test_cleanup_no_pr_list_close(tmp_path):
    """cleanup must NOT call gh pr list/close."""
    from supervisor_cao.cli.acceptance import cleanup
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        with patch("supervisor_cao.cli.acceptance._read_meta", return_value={"repo_dir": str(tmp_path)}):
            with patch("subprocess.run") as mock_run:
                cleanup()
    for call in mock_run.call_args_list:
        cmd = call[0][0] if call[0] else []
        if cmd and cmd[0:2] == ["gh", "pr"]:
            pytest.fail(f"cleanup called gh pr: {cmd}")


def test_purge_evidence_deletes_with_force(tmp_path):
    from supervisor_cao.cli.acceptance import purge_evidence
    ev_dir = tmp_path / "acceptance" / "evidence" / "run-1" / "direct"
    ev_dir.mkdir(parents=True)
    (ev_dir / "result.json").write_text('{}')
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        purge_evidence(force=True)
    assert not ev_dir.exists()


def test_purge_evidence_refuses_without_force(tmp_path):
    from supervisor_cao.cli.acceptance import purge_evidence
    ev_dir = tmp_path / "acceptance" / "evidence" / "run-1" / "direct"
    ev_dir.mkdir(parents=True)
    (ev_dir / "result.json").write_text('{}')
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        purge_evidence(force=False)
    assert ev_dir.exists(), "purge without --force must not delete"


# --- direct scenario pass conditions ---

def _write_valid_pr_content(ev_dir, candidate="c1", task_id="direct",
                            head_branch="agent/direct"):
    from supervisor_cao.pr_content.renderer import render_pr_content
    ev_dir.mkdir(parents=True, exist_ok=True)
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
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))


def test_direct_pass_conditions_all_met(tmp_path):
    """direct PASS requires: READY_FOR_HUMAN_REVIEW + SHAs equal + pr-content valid."""
    from supervisor_cao.cli.acceptance import _direct_pass
    ev_dir = tmp_path / "ev"
    _write_valid_pr_content(ev_dir, candidate="c1", task_id="direct",
                           head_branch="agent/direct")
    ok, _ = _direct_pass("READY_FOR_HUMAN_REVIEW", "c1", "c1", "c1", ev_dir,
                         "direct", "agent/direct")
    assert ok is True


def test_direct_fail_when_not_ready(tmp_path):
    from supervisor_cao.cli.acceptance import _direct_pass
    ev_dir = tmp_path / "ev"
    _write_valid_pr_content(ev_dir)
    ok, reason = _direct_pass("APPROVED", "c1", "c1", "c1", ev_dir,
                              "direct", "agent/direct")
    assert ok is False
    assert "APPROVED" in reason


def test_direct_fail_when_sha_mismatch(tmp_path):
    from supervisor_cao.cli.acceptance import _direct_pass
    ev_dir = tmp_path / "ev"
    _write_valid_pr_content(ev_dir, candidate="c1")
    ok, reason = _direct_pass("READY_FOR_HUMAN_REVIEW", "c1", "c2", "c1", ev_dir,
                              "direct", "agent/direct")
    assert ok is False
    assert "SHA" in reason or "mismatch" in reason


def test_direct_fail_when_pr_content_invalid(tmp_path):
    from supervisor_cao.cli.acceptance import _direct_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir(parents=True)
    # no pr-content files
    ok, reason = _direct_pass("READY_FOR_HUMAN_REVIEW", "c1", "c1", "c1", ev_dir,
                              "direct", "agent/direct")
    assert ok is False
    assert "pr-content" in reason or "pr_content" in reason
