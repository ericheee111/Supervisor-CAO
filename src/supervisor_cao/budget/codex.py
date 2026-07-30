"""Codex call budget manager (spec §8).

Deterministic, persisted budget enforcement. The Supervisor cannot self-track;
this code owns the counts. Each task max 4 Codex calls:
  planner 1 + full_review 1 + incremental_review 1 + judge 1.

On exhaustion: CODEX_BUDGET_EXHAUSTED -> task stops, human required.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "supervisor-cao"

# Default budget per task (spec §8)
DEFAULT_BUDGET = {
    "max_calls_per_task": 4,
    "planner": 1,
    "full_review": 1,
    "incremental_review": 1,
    "judge": 1,
}

VALID_ROLES = {"planner", "full_review", "incremental_review", "judge"}


class BudgetExhausted(Exception):
    """Raised when a Codex role budget is exhausted for a task."""


@dataclass
class CodexCall:
    task_id: str
    role: str            # planner | full_review | incremental_review | judge
    call_index: int      # 1-based index within the role
    input_artifact: str  # path or ref
    output_artifact: str | None
    remaining_budget: int
    candidate_sha: str | None
    ts: float


class CodexBudget:
    """Persisted, thread-safe Codex budget tracker.

    Stores per-task, per-role call counts and a call log in SQLite.
    """

    def __init__(self, db_path: Path | None = None, budget: dict | None = None):
        db_path = db_path or DEFAULT_STATE_DIR / "codex_budget.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = str(db_path)
        self._lock = threading.Lock()
        self._budget = dict(budget) if budget else dict(DEFAULT_BUDGET)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS codex_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    call_index INTEGER NOT NULL,
                    input_artifact TEXT,
                    output_artifact TEXT,
                    remaining_budget INTEGER NOT NULL,
                    candidate_sha TEXT,
                    ts REAL NOT NULL,
                    UNIQUE(task_id, role, call_index)
                )
                """
            )
            c.commit()

    def used(self, task_id: str, role: str) -> int:
        """Return number of Codex calls used for (task_id, role)."""
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role}; expected one of {VALID_ROLES}")
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM codex_calls WHERE task_id=? AND role=?",
                (task_id, role),
            ).fetchone()
        return row["n"]

    def total_used(self, task_id: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM codex_calls WHERE task_id=?", (task_id,)
            ).fetchone()
        return row["n"]

    def remaining(self, task_id: str, role: str | None = None) -> int:
        """Remaining budget for a role, or total remaining if role is None."""
        if role is None:
            return self._budget["max_calls_per_task"] - self.total_used(task_id)
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role}")
        return self._budget[role] - self.used(task_id, role)

    def can_spend(self, task_id: str, role: str) -> bool:
        if self.remaining(task_id, role) <= 0:
            return False
        if self.remaining(task_id) <= 0:
            return False
        return True

    def spend(self, task_id: str, role: str, *, input_artifact: str,
              output_artifact: str | None = None, candidate_sha: str | None = None) -> CodexCall:
        """Record a Codex call. Raises BudgetExhausted if not allowed.

        Atomic across processes: uses BEGIN IMMEDIATE for cross-process safety
        (threading.Lock only protects in-process concurrency). The BEGIN
        IMMEDIATE acquires a database write lock before any reads, preventing
        two processes from reading the same count and both inserting.
        """
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role}; expected one of {VALID_ROLES}")
        with self._lock:
            c = self._conn()
            try:
                # BEGIN IMMEDIATE: acquire write lock before reading counts.
                # This blocks other writers until we commit, ensuring the
                # check-then-insert is atomic across processes.
                c.execute("BEGIN IMMEDIATE")
                role_used = c.execute(
                    "SELECT COUNT(*) AS n FROM codex_calls WHERE task_id=? AND role=?",
                    (task_id, role),
                ).fetchone()["n"]
                total = c.execute(
                    "SELECT COUNT(*) AS n FROM codex_calls WHERE task_id=?", (task_id,)
                ).fetchone()["n"]
                if role_used >= self._budget[role]:
                    c.commit()
                    raise BudgetExhausted(
                        f"CODEX_BUDGET_EXHAUSTED: role {role} used {role_used}/{self._budget[role]} for task {task_id}"
                    )
                if total >= self._budget["max_calls_per_task"]:
                    c.commit()
                    raise BudgetExhausted(
                        f"CODEX_BUDGET_EXHAUSTED: total {total}/{self._budget['max_calls_per_task']} for task {task_id}"
                    )
                call_index = role_used + 1
                remaining = self._budget["max_calls_per_task"] - (total + 1)
                ts = time.time()
                c.execute(
                    "INSERT INTO codex_calls (task_id, role, call_index, input_artifact, output_artifact, remaining_budget, candidate_sha, ts) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (task_id, role, call_index, input_artifact, output_artifact, remaining, candidate_sha, ts),
                )
                c.commit()
                return CodexCall(
                    task_id=task_id, role=role, call_index=call_index,
                    input_artifact=input_artifact, output_artifact=output_artifact,
                    remaining_budget=remaining, candidate_sha=candidate_sha, ts=ts,
                )
            except Exception:
                c.rollback()
                raise
            finally:
                c.close()

    def history(self, task_id: str) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM codex_calls WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "total_used": self.total_used(task_id),
            "max_calls_per_task": self._budget["max_calls_per_task"],
            "remaining_total": self.remaining(task_id),
            "per_role": {r: {"used": self.used(task_id, r), "max": self._budget[r],
                             "remaining": self.remaining(task_id, r)} for r in VALID_ROLES},
        }
