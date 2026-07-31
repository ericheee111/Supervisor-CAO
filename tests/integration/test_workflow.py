"""Integration tests: simulated worker workflows through the policy layer.

These test the deterministic policy layer's behavior with mocked worker
results (spec §20.2). No real CAO/OpenCode/Codex calls. Covers:
- Planner success -> Executor -> Verifier -> Reviewer APPROVED -> Draft PR
- Verifier failure -> fix loop -> reverify -> incremental review
- stale verification (new commit invalidates old result)
- budget exhausted
- pool busy / remote dirty / Windows blocked
"""
from __future__ import annotations

import pytest

from supervisor_cao.state.machine import StateStore, TaskState, ErrorState, IllegalTransition, ShaMismatch
from supervisor_cao.budget.codex import CodexBudget, BudgetExhausted
from supervisor_cao.validation.windows_sync import check_gates, WindowsSyncBlocked, sync as win_sync
from supervisor_cao.validation.remote_pool import ContainerState, select_available


@pytest.fixture
def store(tmp_path):
    return StateStore(db_path=tmp_path / "tasks.db")


@pytest.fixture
def budget(tmp_path):
    return CodexBudget(db_path=tmp_path / "codex.db")


@pytest.fixture
def task(store):
    return store.create("T1", "demo-project", baseline_sha="base1")


def run_to_review_ready(store, task_id, sha="c1"):
    """Helper: drive a task through to REVIEWING with reviewed_sha set."""
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING]:
        s.transition(task_id, st)
    s.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=sha)
    s.transition(task_id, TaskState.LOCAL_VERIFYING)
    s.transition(task_id, TaskState.LOCAL_VERIFIED, tested_sha=sha)
    s.transition(task_id, TaskState.REMOTE_QUEUED)
    s.transition(task_id, TaskState.REMOTE_VERIFYING)
    s.transition(task_id, TaskState.REMOTE_VERIFIED)
    s.transition(task_id, TaskState.REVIEWING, reviewed_sha=sha)
    return s.get(task_id)


# --- Planner success flow ---
def test_planner_success_consumes_one_budget(budget):
    call = budget.spend("T1", "planner", input_artifact="research.md", candidate_sha=None)
    assert call.role == "planner"
    assert budget.remaining("T1", "planner") == 0
    # planner budget exhausted, cannot call again
    with pytest.raises(BudgetExhausted):
        budget.spend("T1", "planner", input_artifact="research2.md")


# --- Executor fix loop ---
def test_executor_fix_loop_reverify(store, task):
    t = run_to_review_ready(store, "T1", "c1")
    store.transition("T1", TaskState.CHANGES_REQUESTED)
    # executor fixes -> new candidate c2
    store.transition("T1", TaskState.FIXING, new_candidate_sha="c2")
    # must re-verify, not skip to APPROVED
    with pytest.raises(IllegalTransition):
        store.transition("T1", TaskState.APPROVED)
    store.transition("T1", TaskState.LOCAL_VERIFYING)
    store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c2")
    store.transition("T1", TaskState.REMOTE_QUEUED)
    store.transition("T1", TaskState.REMOTE_VERIFYING)
    r = store.transition("T1", TaskState.REMOTE_VERIFIED)
    assert r.tested_sha == "c2"


# --- Verifier failure ---
def test_verifier_failure_goes_to_failed(store, task):
    # drive T1 to REMOTE_VERIFYING, then fail verification
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    s.transition("T1", TaskState.LOCAL_VERIFYING)
    s.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    s.transition("T1", TaskState.REMOTE_QUEUED)
    s.transition("T1", TaskState.REMOTE_VERIFYING)
    # verification fails -> FAILED
    r = s.transition("T1", TaskState.FAILED, error="remote verify failed")
    assert r.state == TaskState.FAILED.value
    assert r.error == "remote verify failed"


# --- Stale verification: new commit invalidates ---
def test_stale_verification_blocked(store, task):
    t = run_to_review_ready(store, "T1", "c1")
    # a new commit c2 during FIXING invalidates tested (c1) and reviewed (c1)
    r = store.transition("T1", TaskState.CHANGES_REQUESTED)
    r = store.transition("T1", TaskState.FIXING, new_candidate_sha="c2")
    assert r.tested_sha is None
    assert r.reviewed_sha is None
    assert r.candidate_sha == "c2"
    # cannot use old reviewed_sha c1 for INCREMENTAL_REVIEWING
    store.transition("T1", TaskState.LOCAL_VERIFYING)
    store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c2")
    store.transition("T1", TaskState.REMOTE_QUEUED)
    store.transition("T1", TaskState.REMOTE_VERIFYING)
    store.transition("T1", TaskState.REMOTE_VERIFIED)
    # incremental review needs reviewed_sha == tested_sha (c2)
    store.transition("T1", TaskState.INCREMENTAL_REVIEWING, reviewed_sha="c2")


# --- Budget exhausted stops task ---
def test_budget_exhausted_stops(budget):
    budget.spend("T1", "planner", input_artifact="p")
    budget.spend("T1", "full_review", input_artifact="r")
    budget.spend("T1", "incremental_review", input_artifact="i")
    budget.spend("T1", "judge", input_artifact="j")
    # all 4 used, any further call exhausted
    with pytest.raises(BudgetExhausted):
        budget.spend("T1", "planner", input_artifact="p2")


# --- Pool busy: both containers busy ---
def test_pool_all_busy_no_selection():
    states = [
        ContainerState(name="C1", status="BUSY", locked_by="T1"),
        ContainerState(name="C2", status="BUSY", locked_by="T2"),
    ]
    assert select_available(states) is None


def test_pool_one_available():
    states = [
        ContainerState(name="C1", status="BUSY", locked_by="T1"),
        ContainerState(name="C2", status="AVAILABLE"),
    ]
    chosen = select_available(states)
    assert chosen is not None
    assert chosen.name == "C2"


def test_pool_unhealthy_skipped():
    states = [
        ContainerState(name="C1", status="UNHEALTHY"),
        ContainerState(name="C2", status="DIRTY"),
        ContainerState(name="C3", status="AVAILABLE"),
    ]
    chosen = select_available(states)
    assert chosen.name == "C3"


# --- Windows blocked ---
def test_windows_blocked_when_not_approved(tmp_path):
    import subprocess
    repo = tmp_path / "winrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "i"], check=True)
    with pytest.raises(WindowsSyncBlocked):
        win_sync(str(repo), "agent/T1", "c1", "c1", "c1", False, True)


# --- Full happy path with budget ---
def test_full_happy_path_with_budget(store, budget, task):
    s = store
    b = budget
    # research (no codex)
    s.transition("T1", TaskState.RESEARCHING)
    # plan (codex planner 1/4)
    s.transition("T1", TaskState.PLANNING)
    b.spend("T1", "planner", input_artifact="research.md")
    s.transition("T1", TaskState.PLAN_READY)
    s.transition("T1", TaskState.IMPLEMENTING)
    s.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
    s.transition("T1", TaskState.LOCAL_VERIFYING)
    s.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
    s.transition("T1", TaskState.REMOTE_QUEUED)
    s.transition("T1", TaskState.REMOTE_VERIFYING)
    s.transition("T1", TaskState.REMOTE_VERIFIED)
    s.transition("T1", TaskState.REVIEWING, reviewed_sha="c1")
    # full review (codex 2/4)
    b.spend("T1", "full_review", input_artifact="verification.json", candidate_sha="c1")
    s.transition("T1", TaskState.APPROVED)
    s.transition("T1", TaskState.PR_CONTENT_READY)
    s.transition("T1", TaskState.WINDOWS_SYNCED)
    r = s.transition("T1", TaskState.READY_FOR_HUMAN_REVIEW)
    assert r.state == TaskState.READY_FOR_HUMAN_REVIEW.value
    # only 2 codex calls used (planner + full_review), no fix needed
    assert b.total_used("T1") == 2
