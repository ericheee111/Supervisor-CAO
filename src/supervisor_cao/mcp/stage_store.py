"""Idempotent stage-run store for crash-safe resume (requirement 2).

Each stage of a task is recorded with its status, the CAO terminal_id it used,
the artifact it produced, the candidate SHA at the time, and the Codex budget
call id (if any). On resume after a crash or timeout, ``begin_stage`` returns
the existing COMPLETED record (so the Worker is NOT re-launched, the Codex
budget is NOT re-spent, no duplicate commit/PR/Windows-sync happens). A RUNNING
record whose terminal is still alive is reused; a stale RUNNING record is
reclaimed.

Schema (SQLite, persisted under ~/.local/state/supervisor-cao/stages.db):

    stage_runs(
        task_id TEXT, stage TEXT, stage_run_id TEXT,
        status TEXT,           -- PENDING | RUNNING | COMPLETED | FAILED
        terminal_id TEXT,
        worker_profile TEXT,
        artifact_path TEXT,
        candidate_sha TEXT,
        codex_call_id TEXT,
        started REAL, finished REAL,
        PRIMARY KEY (task_id, stage)
    )

Requirement 2 hard rules (enforced here, in code):
  - COMPLETED stage is never re-run on resume.
  - Codex budget is never re-spent for a COMPLETED stage.
  - No duplicate commit, PR creation, or Windows sync for a COMPLETED stage.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "supervisor-cao"

PENDING = "PENDING"
RUNNING = "RUNNING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

# A RUNNING record older than this (seconds) is considered stale/crashed and
# may be reclaimed. Workers have their own timeouts (up to 600s + headroom),
# so 1 hour is a safe upper bound.
STALE_RUNNING_SECS = 3600


@dataclass
class StageRun:
    task_id: str
    stage: str
    stage_run_id: str
    status: str
    terminal_id: str | None
    worker_profile: str | None
    artifact_path: str | None
    candidate_sha: str | None
    codex_call_id: str | None
    started: float
    finished: float | None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "stage": self.stage,
            "stage_run_id": self.stage_run_id, "status": self.status,
            "terminal_id": self.terminal_id, "worker_profile": self.worker_profile,
            "artifact_path": self.artifact_path, "candidate_sha": self.candidate_sha,
            "codex_call_id": self.codex_call_id,
            "started": self.started, "finished": self.finished,
        }


class StageStore:
    """Persistent, thread-safe stage-run store backing idempotent resume."""

    def __init__(self, db_path: Path | None = None):
        db_path = db_path or DEFAULT_STATE_DIR / "stages.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = str(db_path)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def close(self) -> None:
        """Best-effort close of any pooled connections (Windows file locking)."""
        # sqlite3 connections are created per-call and closed by `with`; this
        # is a no-op kept for API symmetry and test cleanup hooks.
        pass

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS stage_runs (
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    stage_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    terminal_id TEXT,
                    worker_profile TEXT,
                    artifact_path TEXT,
                    candidate_sha TEXT,
                    codex_call_id TEXT,
                    started REAL NOT NULL,
                    finished REAL,
                    PRIMARY KEY (task_id, stage)
                )
                """
            )
            c.commit()

    def begin_stage(self, task_id: str, stage: str,
                    worker_profile: str | None = None) -> tuple[StageRun, bool]:
        """Begin a stage. Returns (stage_run, already_completed).

        - If a COMPLETED record exists: returns it with already_completed=True.
          The caller MUST NOT re-run the Worker or re-spend budget.
        - If a RUNNING record exists and is not stale: returns it with
          already_completed=False (caller reuses its terminal_id).
        - If a RUNNING record is stale: reclaim it (mark FAILED, begin new).
        - Otherwise: insert a new PENDING record and return it.
        """
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM stage_runs WHERE task_id=? AND stage=?",
                (task_id, stage),
            ).fetchone()
            now = time.time()
            if row:
                rec = self._row_to_run(row)
                if rec.status == COMPLETED:
                    return rec, True
                if rec.status == RUNNING:
                    if now - rec.started < STALE_RUNNING_SECS:
                        return rec, False
                    # stale: mark FAILED, fall through to create new
                    c.execute(
                        "UPDATE stage_runs SET status=?, finished=? WHERE task_id=? AND stage=?",
                        (FAILED, now, task_id, stage),
                    )
                # FAILED or PENDING: reclaim by updating in place
                run_id = str(uuid.uuid4())
                c.execute(
                    "UPDATE stage_runs SET stage_run_id=?, status=?, terminal_id=?, "
                    "worker_profile=?, artifact_path=?, candidate_sha=?, codex_call_id=?, "
                    "started=?, finished=NULL WHERE task_id=? AND stage=?",
                    (run_id, PENDING, None, worker_profile, None, None, None, now,
                     task_id, stage),
                )
                c.commit()
                return StageRun(task_id, stage, run_id, PENDING, None, worker_profile,
                                None, None, None, now, None), False
            # no prior record: insert new
            run_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO stage_runs (task_id, stage, stage_run_id, status, terminal_id, "
                "worker_profile, artifact_path, candidate_sha, codex_call_id, started, finished) "
                "VALUES (?,?,?,?,NULL,?,NULL,NULL,NULL,?,NULL)",
                (task_id, stage, run_id, PENDING, worker_profile, now),
            )
            c.commit()
            return StageRun(task_id, stage, run_id, PENDING, None, worker_profile,
                            None, None, None, now, None), False

    def mark_running(self, task_id: str, stage: str, terminal_id: str | None = None,
                     candidate_sha: str | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE stage_runs SET status=?, terminal_id=COALESCE(?, terminal_id), "
                "candidate_sha=COALESCE(?, candidate_sha) WHERE task_id=? AND stage=?",
                (RUNNING, terminal_id, candidate_sha, task_id, stage),
            )
            c.commit()

    def complete_stage(self, task_id: str, stage: str, *,
                       artifact_path: str | None = None,
                       candidate_sha: str | None = None,
                       codex_call_id: str | None = None,
                       terminal_id: str | None = None) -> StageRun:
        with self._lock, self._conn() as c:
            now = time.time()
            c.execute(
                "UPDATE stage_runs SET status=?, artifact_path=COALESCE(?, artifact_path), "
                "candidate_sha=COALESCE(?, candidate_sha), codex_call_id=COALESCE(?, codex_call_id), "
                "terminal_id=COALESCE(?, terminal_id), finished=? WHERE task_id=? AND stage=?",
                (COMPLETED, artifact_path, candidate_sha, codex_call_id, terminal_id,
                 now, task_id, stage),
            )
            c.commit()
            row = c.execute(
                "SELECT * FROM stage_runs WHERE task_id=? AND stage=?",
                (task_id, stage),
            ).fetchone()
            return self._row_to_run(row)

    def fail_stage(self, task_id: str, stage: str, error: str | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE stage_runs SET status=?, finished=? WHERE task_id=? AND stage=?",
                (FAILED, time.time(), task_id, stage),
            )
            c.commit()

    def get(self, task_id: str, stage: str) -> StageRun | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM stage_runs WHERE task_id=? AND stage=?",
                (task_id, stage),
            ).fetchone()
        return self._row_to_run(row) if row else None

    def list_stages(self, task_id: str) -> list[StageRun]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM stage_runs WHERE task_id=? ORDER BY started",
                (task_id,),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def _row_to_run(self, row: sqlite3.Row) -> StageRun:
        return StageRun(
            task_id=row["task_id"], stage=row["stage"],
            stage_run_id=row["stage_run_id"], status=row["status"],
            terminal_id=row["terminal_id"], worker_profile=row["worker_profile"],
            artifact_path=row["artifact_path"], candidate_sha=row["candidate_sha"],
            codex_call_id=row["codex_call_id"], started=row["started"],
            finished=row["finished"],
        )
