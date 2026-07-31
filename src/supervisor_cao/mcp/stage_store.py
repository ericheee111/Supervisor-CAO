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
  - COMPLETED stage with the SAME candidate_sha is never re-run on resume.
  - A COMPLETED stage with a DIFFERENT candidate_sha (after a fix) is stale and
    MUST be re-run — re-verification and incremental review are mandatory.
  - Codex budget is never re-spent for a COMPLETED stage with the same SHA.
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
    attempt: int = 0
    input_sha: str | None = None
    # Worker handle status (separate from task state). A STALLED handle does NOT
    # change the task's TaskState; it records that the worker is stalled. The
    # resume_state saves the task state to restore after a successful reattach.
    handle_status: str | None = None   # RUNNING/COMPLETED/FAILED/STALLED
    resume_state: str | None = None    # task state to restore after reattach
    worker_id: str | None = None       # WorkerMonitor handle id

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "stage": self.stage,
            "stage_run_id": self.stage_run_id, "status": self.status,
            "terminal_id": self.terminal_id, "worker_profile": self.worker_profile,
            "artifact_path": self.artifact_path, "candidate_sha": self.candidate_sha,
            "codex_call_id": self.codex_call_id,
            "started": self.started, "finished": self.finished,
            "attempt": self.attempt, "input_sha": self.input_sha,
            "handle_status": self.handle_status, "resume_state": self.resume_state,
            "worker_id": self.worker_id,
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
                    attempt INTEGER NOT NULL DEFAULT 0,
                    input_sha TEXT,
                    handle_status TEXT,
                    resume_state TEXT,
                    worker_id TEXT,
                    PRIMARY KEY (task_id, stage)
                )
                """
            )
            # migration: add attempt/input_sha columns if upgrading an old DB
            cols = {row[1] for row in c.execute("PRAGMA table_info(stage_runs)")}
            if "attempt" not in cols:
                c.execute("ALTER TABLE stage_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
            if "input_sha" not in cols:
                c.execute("ALTER TABLE stage_runs ADD COLUMN input_sha TEXT")
            if "handle_status" not in cols:
                c.execute("ALTER TABLE stage_runs ADD COLUMN handle_status TEXT")
            if "resume_state" not in cols:
                c.execute("ALTER TABLE stage_runs ADD COLUMN resume_state TEXT")
            if "worker_id" not in cols:
                c.execute("ALTER TABLE stage_runs ADD COLUMN worker_id TEXT")
            c.commit()

    def begin_stage(self, task_id: str, stage: str,
                    worker_profile: str | None = None,
                    candidate_sha: str | None = None,
                    input_sha: str | None = None) -> tuple[StageRun, bool]:
        """Begin a stage. Returns (stage_run, already_completed).

        - If a COMPLETED record exists with the SAME candidate_sha: returns it
          with already_completed=True. The caller MUST NOT re-run the Worker or
          re-spend budget. (Idempotent resume.)
        - If a COMPLETED record exists with a DIFFERENT candidate_sha (a fix
          produced a new SHA): the old result is stale. Reclaim the record,
          increment the attempt counter, and re-run. This enforces "after a fix,
          re-verification and incremental review are mandatory" (spec §9) and
          supports multiple CHANGES_REQUESTED rounds.
        - If a RUNNING record exists and is not stale: returns it with
          already_completed=False (caller reuses its terminal_id and the already-
          spent Codex budget — no duplicate call/commit/PR).
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
                    # Same candidate_sha -> idempotent skip. Different -> stale, re-run.
                    if candidate_sha is None or rec.candidate_sha == candidate_sha:
                        return rec, True
                    # stale: a new candidate invalidates this COMPLETED record.
                    # Fall through to reclaim (attempt++).
                if rec.status == RUNNING:
                    if now - rec.started < STALE_RUNNING_SECS:
                        return rec, False
                    # stale: mark FAILED, fall through to create new
                    c.execute(
                        "UPDATE stage_runs SET status=?, finished=? WHERE task_id=? AND stage=?",
                        (FAILED, now, task_id, stage),
                    )
                # FAILED, PENDING, or stale-COMPLETED: reclaim by updating in place.
                # Increment attempt so each re-run (e.g. each CHANGES_REQUESTED round)
                # is recorded distinctly.
                run_id = str(uuid.uuid4())
                next_attempt = (rec.attempt or 0) + 1 if rec.status != PENDING else (rec.attempt or 0)
                c.execute(
                    "UPDATE stage_runs SET stage_run_id=?, status=?, terminal_id=?, "
                    "worker_profile=?, artifact_path=?, candidate_sha=?, codex_call_id=?, "
                    "started=?, finished=NULL, attempt=?, input_sha=?, "
                    "handle_status=NULL, resume_state=NULL, worker_id=NULL "
                    "WHERE task_id=? AND stage=?",
                    (run_id, PENDING, None, worker_profile, None, None, None, now,
                     next_attempt, input_sha, task_id, stage),
                )
                c.commit()
                return StageRun(task_id, stage, run_id, PENDING, None, worker_profile,
                                None, None, None, now, None, next_attempt, input_sha,
                                None, None, None), False
            # no prior record: insert new
            run_id = str(uuid.uuid4())
            c.execute(
                "INSERT INTO stage_runs (task_id, stage, stage_run_id, status, terminal_id, "
                "worker_profile, artifact_path, candidate_sha, codex_call_id, started, finished, "
                "attempt, input_sha, handle_status, resume_state, worker_id) "
                "VALUES (?,?,?,?,NULL,?,NULL,NULL,NULL,?,NULL,0,?,NULL,NULL,NULL)",
                (task_id, stage, run_id, PENDING, worker_profile, now, input_sha),
            )
            c.commit()
            return StageRun(task_id, stage, run_id, PENDING, None, worker_profile,
                            None, None, None, now, None, 0, input_sha, None, None, None), False

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

    def set_handle_status(self, task_id: str, stage: str, *,
                          handle_status: str | None = None,
                          resume_state: str | None = None,
                          worker_id: str | None = None) -> None:
        """Update the worker handle status on a stage run. handle_status is one
        of RUNNING/COMPLETED/FAILED/STALLED (worker-level, NOT task state).
        resume_state saves the task state to restore after a successful reattach.
        worker_id is the WorkerMonitor handle id for resume lookup."""
        with self._lock, self._conn() as c:
            sets: list[str] = []
            vals: list = []
            if handle_status is not None:
                sets.append("handle_status=?")
                vals.append(handle_status)
            if resume_state is not None:
                sets.append("resume_state=?")
                vals.append(resume_state)
            if worker_id is not None:
                sets.append("worker_id=?")
                vals.append(worker_id)
            if not sets:
                return
            vals.extend([task_id, stage])
            c.execute(
                f"UPDATE stage_runs SET {', '.join(sets)} WHERE task_id=? AND stage=?",
                vals,
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
        keys = row.keys()
        return StageRun(
            task_id=row["task_id"], stage=row["stage"],
            stage_run_id=row["stage_run_id"], status=row["status"],
            terminal_id=row["terminal_id"], worker_profile=row["worker_profile"],
            artifact_path=row["artifact_path"], candidate_sha=row["candidate_sha"],
            codex_call_id=row["codex_call_id"], started=row["started"],
            finished=row["finished"],
            attempt=row["attempt"] if "attempt" in keys else 0,
            input_sha=row["input_sha"] if "input_sha" in keys else None,
            handle_status=row["handle_status"] if "handle_status" in keys else None,
            resume_state=row["resume_state"] if "resume_state" in keys else None,
            worker_id=row["worker_id"] if "worker_id" in keys else None,
        )
