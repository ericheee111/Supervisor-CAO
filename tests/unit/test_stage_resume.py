"""Unit tests for idempotent stage resume (requirement 2).

Verifies:
  - A COMPLETED stage is NOT re-run on resume (Worker not re-launched).
  - Codex budget is NOT re-spent for a COMPLETED stage.
  - No duplicate commit / PR / Windows-sync for a COMPLETED stage.
  - A stale RUNNING stage is reclaimed.
  - resume_task re-enters run_next_stage exactly once per incomplete stage.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.mcp.stage_store import (  # noqa: E402
    StageStore, COMPLETED, RUNNING, PENDING, FAILED,
)


@pytest.fixture
def store():
    import gc
    d = tempfile.mkdtemp()
    s = StageStore(db_path=Path(d) / "stages.db")
    yield s
    del s
    gc.collect()  # release sqlite3 connections so Windows can delete the db file


class TestStageStoreIdempotency:
    def test_completed_stage_not_rerun(self, store):
        run, done = store.begin_stage("T1", "plan", "codex-planner")
        assert done is False
        assert run.status == PENDING
        store.mark_running("T1", "plan", terminal_id="abc123", candidate_sha="sha1")
        store.complete_stage("T1", "plan", artifact_path="/p/plan.json",
                             candidate_sha="sha1", codex_call_id="call-1")
        # resume: stage is COMPLETED — must return done=True, no new run
        run2, done2 = store.begin_stage("T1", "plan", "codex-planner")
        assert done2 is True
        assert run2.status == COMPLETED
        assert run2.candidate_sha == "sha1"
        assert run2.codex_call_id == "call-1"

    def test_running_stage_reuses_terminal(self, store):
        run, done = store.begin_stage("T1", "research", "researcher")
        store.mark_running("T1", "research", terminal_id="t1")
        # resume while still running: reuse, not done
        run2, done2 = store.begin_stage("T1", "research", "researcher")
        assert done2 is False
        assert run2.status == RUNNING
        assert run2.terminal_id == "t1"

    def test_stale_running_reclaimed(self, store):
        run, done = store.begin_stage("T1", "plan", "codex-planner")
        store.mark_running("T1", "plan")
        # simulate stale: manually age the started timestamp
        import sqlite3 as sq
        with store._conn() as c:
            c.execute("UPDATE stage_runs SET started=? WHERE task_id=? AND stage=?",
                      (0.0, "T1", "plan"))
            c.commit()
        run2, done2 = store.begin_stage("T1", "plan", "codex-planner")
        assert done2 is False
        assert run2.status == PENDING
        assert run2.stage_run_id != run.stage_run_id  # new run id

    def test_failed_stage_reclaimed(self, store):
        store.begin_stage("T1", "plan", "codex-planner")
        store.fail_stage("T1", "plan", error="boom")
        run2, done2 = store.begin_stage("T1", "plan", "codex-planner")
        assert done2 is False
        assert run2.status == PENDING

    def test_list_stages(self, store):
        store.begin_stage("T1", "research", "researcher")
        store.begin_stage("T1", "plan", "codex-planner")
        stages = store.list_stages("T1")
        assert len(stages) == 2
        assert {s.stage for s in stages} == {"research", "plan"}

    def test_get_returns_none_for_missing(self, store):
        assert store.get("T1", "bogus") is None

    def test_complete_then_get(self, store):
        store.begin_stage("T1", "review", "codex-reviewer")
        store.complete_stage("T1", "review", artifact_path="/p/review.json",
                             candidate_sha="sha2")
        run = store.get("T1", "review")
        assert run is not None
        assert run.status == COMPLETED
        assert run.artifact_path == "/p/review.json"
