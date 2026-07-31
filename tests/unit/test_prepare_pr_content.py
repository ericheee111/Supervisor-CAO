"""Unit tests for PolicyGateway.prepare_pr_content and deprecated wrapper."""
import json
import warnings
from pathlib import Path

import pytest

from supervisor_cao.state.machine import StateStore, TaskState
from supervisor_cao.mcp.policy_gateway import PolicyGateway, PolicyError
from supervisor_cao.mcp.stage_store import StageStore
from supervisor_cao.budget.codex import CodexBudget


@pytest.fixture
def dirs(tmp_path):
    return {"state": tmp_path / "state", "runs": tmp_path / "runs",
            "stages": tmp_path / "stages", "budget": tmp_path / "budget",
            "workers": tmp_path / "workers"}


@pytest.fixture
def gw(dirs):
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    store = StateStore(db_path=dirs["state"] / "tasks.db")
    budget = CodexBudget(db_path=dirs["budget"] / "codex.db")
    stages = StageStore(db_path=dirs["stages"] / "stages.db")
    return PolicyGateway(state_store=store, budget=budget, stage_store=stages,
                         test_mode=True), store


def _drive_to_approved(store, task_id="T1", sha="abc123"):
    """Helper: drive a task through the happy path to APPROVED."""
    s = store
    s.create(task_id, "demo")
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
    return s.transition(task_id, TaskState.APPROVED)


def test_prepare_pr_content_rejects_not_approved(gw):
    gateway, store = gw
    store.create("T1", "demo")
    with pytest.raises(PolicyError, match="not APPROVED"):
        gateway.prepare_pr_content("T1", "demo")


def test_prepare_pr_content_rejects_sha_mismatch(gw):
    gateway, store = gw
    _drive_to_approved(store, sha="aaa")
    # break SHA equality
    import sqlite3
    with sqlite3.connect(str(store._db)) as c:
        c.execute("UPDATE tasks SET candidate_sha='bbb', reviewed_sha='aaa' WHERE task_id='T1'")
        c.commit()
    with pytest.raises(PolicyError, match="reviewed.*!=.*candidate"):
        gateway.prepare_pr_content("T1", "demo")


def test_prepare_pr_content_succeeds_when_approved(gw):
    gateway, store = gw
    _drive_to_approved(store, sha="abc123")
    result = gateway.prepare_pr_content("T1", "demo")
    assert result["status"] == "PR_CONTENT_READY"
    assert result["candidate_sha"] == "abc123"


def test_create_draft_pr_is_deprecated_wrapper(gw):
    """create_draft_pr should emit DeprecationWarning and delegate."""
    gateway, store = gw
    store.create("T1", "demo")
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with pytest.raises(PolicyError):
            gateway.create_draft_pr("T1", "demo")
        assert any(issubclass(x.category, DeprecationWarning) for x in w)


def test_prepare_pr_content_does_not_access_network(gw):
    """prepare_pr_content must not call subprocess or network."""
    gateway, store = gw
    _drive_to_approved(store)
    import inspect
    src = inspect.getsource(gateway.prepare_pr_content)
    assert "subprocess" not in src
    assert "requests" not in src
    assert "urllib" not in src
