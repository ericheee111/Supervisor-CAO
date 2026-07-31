"""Unit tests for the task state machine (spec §9, §20.1)."""
import tempfile
from pathlib import Path

import pytest

from supervisor_cao.state.machine import (
    StateStore, TaskState, ErrorState, IllegalTransition, ShaMismatch,
)


@pytest.fixture
def store(tmp_path):
    return StateStore(db_path=tmp_path / "tasks.db")


@pytest.fixture
def task(store):
    return store.create("T1", "demo-project", baseline_sha="aaa111")


# --- legal transitions ---
def test_create_sets_created(store, task):
    assert task.state == TaskState.CREATED.value
    assert task.baseline_sha == "aaa111"


def test_legal_happy_path(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.PLANNING)
    store.transition("T1", TaskState.PLAN_READY)
    store.transition("T1", TaskState.IMPLEMENTING)
    r = store.transition("T1", TaskState.IMPLEMENTED)
    assert r.state == TaskState.IMPLEMENTED.value


def test_illegal_skip_rejected(store, task):
    # cannot skip RESEARCHING/PLANNING straight to IMPLEMENTED
    with pytest.raises(IllegalTransition):
        store.transition("T1", TaskState.IMPLEMENTED)


def test_illegal_backward_rejected(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    with pytest.raises(IllegalTransition):
        store.transition("T1", TaskState.CREATED)


def test_error_state_reachable_from_any(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    # any non-terminal -> error
    r = store.transition("T1", ErrorState.CODEX_BUDGET_EXHAUSTED.value,
                         error=ErrorState.CODEX_BUDGET_EXHAUSTED.value)
    assert r.error == ErrorState.CODEX_BUDGET_EXHAUSTED.value


# --- SHA matching rules ---
def test_new_candidate_invalidates_tested_reviewed(store, task):
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    s.transition("T1", TaskState.LOCAL_VERIFYING)
    s.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    s.transition("T1", TaskState.REMOTE_QUEUED)
    s.transition("T1", TaskState.REMOTE_VERIFYING)
    s.transition("T1", TaskState.REMOTE_VERIFIED)
    s.transition("T1", TaskState.REVIEWING, reviewed_sha="c1")
    s.transition("T1", TaskState.CHANGES_REQUESTED)
    # a new commit during FIXING invalidates tested/reviewed
    r = s.transition("T1", TaskState.FIXING, new_candidate_sha="c2")
    assert r.candidate_sha == "c2"
    assert r.tested_sha is None
    assert r.reviewed_sha is None


def test_tested_must_equal_candidate(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.PLANNING)
    store.transition("T1", TaskState.PLAN_READY)
    store.transition("T1", TaskState.IMPLEMENTING)
    store.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    store.transition("T1", TaskState.LOCAL_VERIFYING)
    with pytest.raises(ShaMismatch):
        store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="WRONG")


def test_reviewed_must_equal_tested(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.PLANNING)
    store.transition("T1", TaskState.PLAN_READY)
    store.transition("T1", TaskState.IMPLEMENTING)
    store.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    store.transition("T1", TaskState.LOCAL_VERIFYING)
    store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    store.transition("T1", TaskState.REMOTE_QUEUED)
    store.transition("T1", TaskState.REMOTE_VERIFYING)
    store.transition("T1", TaskState.REMOTE_VERIFIED)
    with pytest.raises(ShaMismatch):
        store.transition("T1", TaskState.REVIEWING, reviewed_sha="WRONG")


def test_approved_requires_reviewed_eq_tested(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.PLANNING)
    store.transition("T1", TaskState.PLAN_READY)
    store.transition("T1", TaskState.IMPLEMENTING)
    store.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    store.transition("T1", TaskState.LOCAL_VERIFYING)
    store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    store.transition("T1", TaskState.REMOTE_QUEUED)
    store.transition("T1", TaskState.REMOTE_VERIFYING)
    store.transition("T1", TaskState.REMOTE_VERIFIED)
    store.transition("T1", TaskState.REVIEWING)
    with pytest.raises(ShaMismatch):
        store.transition("T1", TaskState.APPROVED)  # no reviewed_sha set


def test_full_happy_path_with_sha(store, task):
    s = store
    s.transition("T1", TaskState.RESEARCHING)
    s.transition("T1", TaskState.PLANNING)
    s.transition("T1", TaskState.PLAN_READY)
    s.transition("T1", TaskState.IMPLEMENTING)
    s.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    s.transition("T1", TaskState.LOCAL_VERIFYING)
    s.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    s.transition("T1", TaskState.REMOTE_QUEUED)
    s.transition("T1", TaskState.REMOTE_VERIFYING)
    s.transition("T1", TaskState.REMOTE_VERIFIED)
    s.transition("T1", TaskState.REVIEWING, reviewed_sha="c1")
    s.transition("T1", TaskState.APPROVED)
    s.transition("T1", TaskState.DRAFT_PR_CREATED)
    s.transition("T1", TaskState.WINDOWS_SYNCED)
    r = s.transition("T1", TaskState.READY_FOR_HUMAN_REVIEW)
    assert r.state == TaskState.READY_FOR_HUMAN_REVIEW.value


# --- fix loop requires re-verification ---
def test_fix_requires_reverify(store, task):
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    s.transition("T1", TaskState.LOCAL_VERIFYING)
    s.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    s.transition("T1", TaskState.REMOTE_QUEUED)
    s.transition("T1", TaskState.REMOTE_VERIFYING)
    s.transition("T1", TaskState.REMOTE_VERIFIED)
    s.transition("T1", TaskState.REVIEWING, reviewed_sha="c1")
    s.transition("T1", TaskState.CHANGES_REQUESTED)
    s.transition("T1", TaskState.FIXING, new_candidate_sha="c2")
    # after fix must go back to LOCAL_VERIFYING (re-verify), not skip to APPROVED
    with pytest.raises(IllegalTransition):
        s.transition("T1", TaskState.APPROVED)
    s.transition("T1", TaskState.LOCAL_VERIFYING)


# --- events / audit ---
def test_events_logged(store, task):
    store.transition("T1", TaskState.RESEARCHING)
    events = store.events("T1")
    assert len(events) >= 2  # CREATE + TRANSITION
    assert events[0]["event"] == "CREATE"
    assert events[1]["event"] == "TRANSITION"


# --- NEEDS_HUMAN coverage (R1: STALLED is NOT a TaskState; NEEDS_HUMAN is) ---

def test_needs_human_reachable_from_non_terminal(store, task):
    """NEEDS_HUMAN is reachable from any non-terminal state."""
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.NEEDS_HUMAN)
    rec = store.get("T1")
    assert rec.state == TaskState.NEEDS_HUMAN.value


def test_needs_human_reachable_from_failed(store, task):
    """FAILED → NEEDS_HUMAN is a legal transition."""
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.FAILED, error="something broke")
    store.transition("T1", TaskState.NEEDS_HUMAN)
    rec = store.get("T1")
    assert rec.state == TaskState.NEEDS_HUMAN.value


def test_needs_human_is_terminal(store, task):
    """NEEDS_HUMAN is a terminal state — no outgoing transitions."""
    store.transition("T1", TaskState.RESEARCHING)
    store.transition("T1", TaskState.NEEDS_HUMAN)
    # Any transition out of NEEDS_HUMAN should be illegal
    with pytest.raises(IllegalTransition):
        store.transition("T1", TaskState.RESEARCHING)


def test_stalled_not_in_taskstate():
    """STALLED is a WorkerHandle status, NOT a TaskState (R1)."""
    values = {t.value for t in TaskState}
    assert "STALLED" not in values
