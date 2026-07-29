"""Unit tests for the Codex budget manager (spec §8, §20.1)."""
import pytest

from supervisor_cao.budget.codex import CodexBudget, BudgetExhausted, DEFAULT_BUDGET


@pytest.fixture
def budget(tmp_path):
    return CodexBudget(db_path=tmp_path / "codex.db")


def test_default_budget_values():
    assert DEFAULT_BUDGET["max_calls_per_task"] == 4
    assert DEFAULT_BUDGET["planner"] == 1
    assert DEFAULT_BUDGET["full_review"] == 1
    assert DEFAULT_BUDGET["incremental_review"] == 1
    assert DEFAULT_BUDGET["judge"] == 1


def test_spend_planner(budget):
    call = budget.spend("T1", "planner", input_artifact="plan.md", candidate_sha="c1")
    assert call.role == "planner"
    assert call.call_index == 1
    assert call.remaining_budget == 3
    assert budget.used("T1", "planner") == 1
    assert budget.remaining("T1", "planner") == 0


def test_role_exhausted(budget):
    budget.spend("T1", "planner", input_artifact="p")
    with pytest.raises(BudgetExhausted):
        budget.spend("T1", "planner", input_artifact="p2")


def test_total_exhausted(tmp_path):
    # custom budget: 2 total, 1 each for 2 roles
    b = CodexBudget(db_path=tmp_path / "codex.db",
                    budget={"max_calls_per_task": 2, "planner": 1, "full_review": 1,
                            "incremental_review": 1, "judge": 1})
    b.spend("T1", "planner", input_artifact="p")
    b.spend("T1", "full_review", input_artifact="r")
    with pytest.raises(BudgetExhausted):
        b.spend("T1", "incremental_review", input_artifact="i")


def test_invalid_role_rejected(budget):
    with pytest.raises(ValueError):
        budget.spend("T1", "supervisor", input_artifact="x")


def test_remaining_and_summary(budget):
    budget.spend("T1", "planner", input_artifact="p")
    s = budget.summary("T1")
    assert s["total_used"] == 1
    assert s["remaining_total"] == 3
    assert s["per_role"]["planner"]["used"] == 1
    assert s["per_role"]["planner"]["remaining"] == 0
    assert s["per_role"]["full_review"]["remaining"] == 1


def test_history_order(budget):
    budget.spend("T1", "planner", input_artifact="p")
    budget.spend("T1", "full_review", input_artifact="r")
    h = budget.history("T1")
    assert len(h) == 2
    assert h[0]["role"] == "planner"
    assert h[1]["role"] == "full_review"


def test_separate_tasks_independent(budget):
    budget.spend("T1", "planner", input_artifact="p")
    assert budget.used("T2", "planner") == 0
    budget.spend("T2", "planner", input_artifact="p2")
    assert budget.used("T1", "planner") == 1
    assert budget.used("T2", "planner") == 1
