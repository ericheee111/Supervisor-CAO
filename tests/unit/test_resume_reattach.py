"""Unit tests for resume pass conditions and strict assertions.

These tests verify the _resume_pass logic and the strict assertions that
replace the old loose checks (budget_not_respent >=, candidate_unchanged tautology).
The actual controller-crash + reattach flow requires a live cao-server and is
exercised by the E2E acceptance scenario.
"""
import json
from pathlib import Path

import pytest


def test_resume_pass_all_strict_conditions(tmp_path):
    """resume PASS requires strict == on budget/stage attempts (no >=)."""
    from supervisor_cao.cli.acceptance import _resume_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir(parents=True)
    # write valid pr-content
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {"plan": {"steps": [{"description": "x"}]},
            "implementation": {"candidate_sha": "c1", "changed_files": ["a.py"]},
            "verification": {"candidate_sha": "c1", "tested_sha": "c1", "wsl_results": {}, "remote_results": {}},
            "review": {"reviewed_sha": "c1", "decision": "APPROVED", "findings": []},
            "budget": {"total_used": 1, "remaining": 3}}
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/resume",
            "pushed_sha": "c1", "push_succeeded": True}
    j, m, s = render_pr_content(arts, "resume", "main", "agent/resume", push)
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))
    ok, _ = _resume_pass(
        final_state="READY_FOR_HUMAN_REVIEW",
        budget_before_used=2, budget_after_used=2,  # strict == (no increase)
        stages_before=[{"stage": "research", "status": "COMPLETED", "attempt": 1, "candidate_sha": "c1"}],
        stages_after=[{"stage": "research", "status": "COMPLETED", "attempt": 1, "candidate_sha": "c1"},
                      {"stage": "plan", "status": "COMPLETED", "attempt": 1, "candidate_sha": "c1"}],
        pr_content_sha256_before="abc123", pr_content_sha256_after="abc123",
        windows_sync_attempt_before=None, windows_sync_attempt_after=1,
        ev_dir=ev_dir, candidate="c1", tested="c1", reviewed="c1",
        task_id="resume", head_branch="agent/resume")
    assert ok is True


def test_resume_fail_when_budget_increased(tmp_path):
    """Budget must NOT increase for completed stages (strict ==, not >=)."""
    from supervisor_cao.cli.acceptance import _resume_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir(parents=True)
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {"plan": {"steps": [{"description": "x"}]},
            "implementation": {"candidate_sha": "c1", "changed_files": ["a.py"]},
            "verification": {"candidate_sha": "c1", "tested_sha": "c1", "wsl_results": {}, "remote_results": {}},
            "review": {"reviewed_sha": "c1", "decision": "APPROVED", "findings": []},
            "budget": {"total_used": 1, "remaining": 3}}
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/resume",
            "pushed_sha": "c1", "push_succeeded": True}
    j, m, s = render_pr_content(arts, "resume", "main", "agent/resume", push)
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))
    # budget increased from 2 to 3 — should FAIL (old code used >= which passed)
    ok, reason = _resume_pass(
        final_state="READY_FOR_HUMAN_REVIEW",
        budget_before_used=2, budget_after_used=3,
        stages_before=[], stages_after=[],
        pr_content_sha256_before=None, pr_content_sha256_after=None,
        windows_sync_attempt_before=None, windows_sync_attempt_after=1,
        ev_dir=ev_dir, candidate="c1", tested="c1", reviewed="c1",
        task_id="resume", head_branch="agent/resume")
    assert ok is False
    assert "budget" in reason.lower()


def test_resume_fail_when_stage_attempt_increased(tmp_path):
    """Completed stage attempt must NOT increase (strict ==)."""
    from supervisor_cao.cli.acceptance import _resume_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir(parents=True)
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {"plan": {"steps": [{"description": "x"}]},
            "implementation": {"candidate_sha": "c1", "changed_files": ["a.py"]},
            "verification": {"candidate_sha": "c1", "tested_sha": "c1", "wsl_results": {}, "remote_results": {}},
            "review": {"reviewed_sha": "c1", "decision": "APPROVED", "findings": []},
            "budget": {"total_used": 1, "remaining": 3}}
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/resume",
            "pushed_sha": "c1", "push_succeeded": True}
    j, m, s = render_pr_content(arts, "resume", "main", "agent/resume", push)
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))
    # research stage attempt went from 1 to 2 — should FAIL
    ok, reason = _resume_pass(
        final_state="READY_FOR_HUMAN_REVIEW",
        budget_before_used=2, budget_after_used=2,
        stages_before=[{"stage": "research", "status": "COMPLETED", "attempt": 1, "candidate_sha": "c1"}],
        stages_after=[{"stage": "research", "status": "COMPLETED", "attempt": 2, "candidate_sha": "c1"}],
        pr_content_sha256_before=None, pr_content_sha256_after=None,
        windows_sync_attempt_before=None, windows_sync_attempt_after=1,
        ev_dir=ev_dir, candidate="c1", tested="c1", reviewed="c1",
        task_id="resume", head_branch="agent/resume")
    assert ok is False
    assert "attempt" in reason.lower() or "stage" in reason.lower()


def test_resume_fail_when_pr_content_sha256_changed(tmp_path):
    """PR content package must NOT be regenerated (sha256 must match)."""
    from supervisor_cao.cli.acceptance import _resume_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir(parents=True)
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {"plan": {"steps": [{"description": "x"}]},
            "implementation": {"candidate_sha": "c1", "changed_files": ["a.py"]},
            "verification": {"candidate_sha": "c1", "tested_sha": "c1", "wsl_results": {}, "remote_results": {}},
            "review": {"reviewed_sha": "c1", "decision": "APPROVED", "findings": []},
            "budget": {"total_used": 1, "remaining": 3}}
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/resume",
            "pushed_sha": "c1", "push_succeeded": True}
    j, m, s = render_pr_content(arts, "resume", "main", "agent/resume", push)
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))
    ok, reason = _resume_pass(
        final_state="READY_FOR_HUMAN_REVIEW",
        budget_before_used=2, budget_after_used=2,
        stages_before=[], stages_after=[],
        pr_content_sha256_before="abc123", pr_content_sha256_after="DIFFERENT",
        windows_sync_attempt_before=None, windows_sync_attempt_after=1,
        ev_dir=ev_dir, candidate="c1", tested="c1", reviewed="c1",
        task_id="resume", head_branch="agent/resume")
    assert ok is False
    assert "pr-content" in reason.lower() or "sha256" in reason.lower()


def test_resume_fail_when_not_ready(tmp_path):
    from supervisor_cao.cli.acceptance import _resume_pass
    ok, reason = _resume_pass(
        final_state="NEEDS_HUMAN",
        budget_before_used=1, budget_after_used=1,
        stages_before=[], stages_after=[],
        pr_content_sha256_before=None, pr_content_sha256_after=None,
        windows_sync_attempt_before=None, windows_sync_attempt_after=None,
        ev_dir=tmp_path, candidate="c1", tested="c1", reviewed="c1",
        task_id="resume", head_branch="agent/resume")
    assert ok is False
    assert "NEEDS_HUMAN" in reason or "final_state" in reason
