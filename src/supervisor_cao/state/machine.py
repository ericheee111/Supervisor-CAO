"""Deterministic task state machine for Supervisor-CAO.

Enforces legal state transitions, SHA matching rules, and the hard constraints
from the design spec (§9). Prompts explain these rules; THIS code enforces them.

Hard rules (spec §9):
- Any new commit invalidates old verification and review.
- tested SHA must equal candidate SHA.
- reviewed SHA must equal tested SHA.
- after a fix, re-verification is mandatory.
- no skipping states.
- natural-language "passed" cannot replace artifacts and exit codes.
- state is persisted (SQLite or atomic JSON).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "supervisor-cao"


class TaskState(str, Enum):
    # Happy path
    CREATED = "CREATED"
    RESEARCHING = "RESEARCHING"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    IMPLEMENTING = "IMPLEMENTING"
    IMPLEMENTED = "IMPLEMENTED"
    LOCAL_VERIFYING = "LOCAL_VERIFYING"
    LOCAL_VERIFIED = "LOCAL_VERIFIED"
    REMOTE_QUEUED = "REMOTE_QUEUED"
    REMOTE_VERIFYING = "REMOTE_VERIFYING"
    REMOTE_VERIFIED = "REMOTE_VERIFIED"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    FIXING = "FIXING"
    INCREMENTAL_REVIEWING = "INCREMENTAL_REVIEWING"
    APPROVED = "APPROVED"
    PR_CONTENT_READY = "PR_CONTENT_READY"
    DRAFT_PR_CREATED = "DRAFT_PR_CREATED"  # legacy, retained for decode only
    WINDOWS_SYNCED = "WINDOWS_SYNCED"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    # Terminal / paused
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class ErrorState(str, Enum):
    LOCAL_WORKTREE_DIRTY = "LOCAL_WORKTREE_DIRTY"
    REMOTE_WORKTREE_DIRTY = "REMOTE_WORKTREE_DIRTY"
    REMOTE_POOL_UNAVAILABLE = "REMOTE_POOL_UNAVAILABLE"
    REMOTE_ENV_LOCK_TIMEOUT = "REMOTE_ENV_LOCK_TIMEOUT"
    STALE_VERIFICATION = "STALE_VERIFICATION"
    CODEX_BUDGET_EXHAUSTED = "CODEX_BUDGET_EXHAUSTED"
    NO_PROGRESS = "NO_PROGRESS"
    WINDOWS_SYNC_BLOCKED = "WINDOWS_SYNC_BLOCKED"
    PR_CREATION_FAILED = "PR_CREATION_FAILED"
    PR_CONTENT_GENERATION_FAILED = "PR_CONTENT_GENERATION_FAILED"
    MODEL_CONFIG_INVALID = "MODEL_CONFIG_INVALID"
    CAO_PROVIDER_INCOMPATIBLE = "CAO_PROVIDER_INCOMPATIBLE"


# Legal forward transitions (spec §9 workflow). Each state -> set of allowed
# next states. Error states are reachable from many states (handled separately).
TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.RESEARCHING, TaskState.PLANNING, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.RESEARCHING: {TaskState.PLANNING, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.PLANNING: {TaskState.PLAN_READY, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.PLAN_READY: {TaskState.IMPLEMENTING, TaskState.NEEDS_HUMAN},
    TaskState.IMPLEMENTING: {TaskState.IMPLEMENTED, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.IMPLEMENTED: {TaskState.LOCAL_VERIFYING, TaskState.FAILED},
    TaskState.LOCAL_VERIFYING: {TaskState.LOCAL_VERIFIED, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.LOCAL_VERIFIED: {TaskState.REMOTE_QUEUED, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.REMOTE_QUEUED: {TaskState.REMOTE_VERIFYING, TaskState.FAILED},
    TaskState.REMOTE_VERIFYING: {TaskState.REMOTE_VERIFIED, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.REMOTE_VERIFIED: {TaskState.REVIEWING, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.REVIEWING: {TaskState.APPROVED, TaskState.CHANGES_REQUESTED, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.CHANGES_REQUESTED: {TaskState.FIXING, TaskState.NEEDS_HUMAN},
    # NO_PROGRESS is an ErrorState (set via error field), not a TaskState.
    # FIXING returns to LOCAL_VERIFYING for mandatory re-verification (spec §9).
    TaskState.FIXING: {TaskState.LOCAL_VERIFYING, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.INCREMENTAL_REVIEWING: {TaskState.APPROVED, TaskState.CHANGES_REQUESTED, TaskState.FAILED, TaskState.NEEDS_HUMAN},
    TaskState.APPROVED: {TaskState.PR_CONTENT_READY, TaskState.FAILED},
    # DRAFT_PR_CREATED retained as legacy terminal-paused (no inbound, no forward).
    TaskState.DRAFT_PR_CREATED: set(),
    TaskState.PR_CONTENT_READY: {TaskState.WINDOWS_SYNCED, TaskState.FAILED},
    TaskState.WINDOWS_SYNCED: {TaskState.READY_FOR_HUMAN_REVIEW, TaskState.FAILED},
    TaskState.READY_FOR_HUMAN_REVIEW: set(),  # terminal success
    TaskState.FAILED: {TaskState.NEEDS_HUMAN},  # failed -> human
    TaskState.NEEDS_HUMAN: set(),  # terminal paused
}

# CHANGES_REQUESTED and FIXING also lead into INCREMENTAL_REVIEWING after re-verify.
# We model: FIXING -> LOCAL_VERIFYING -> ... -> REMOTE_VERIFIED -> INCREMENTAL_REVIEWING
TRANSITIONS[TaskState.REMOTE_VERIFIED].add(TaskState.INCREMENTAL_REVIEWING)
TRANSITIONS[TaskState.CHANGES_REQUESTED].add(TaskState.FIXING)

# Any non-terminal state may transition to an error/terminal state.
ERROR_STATES = {e.value for e in ErrorState}


@dataclass
class TaskRecord:
    task_id: str
    project: str
    state: str = TaskState.CREATED.value
    baseline_sha: str | None = None
    candidate_sha: str | None = None
    tested_sha: str | None = None
    reviewed_sha: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class IllegalTransition(Exception):
    pass


class ShaMismatch(Exception):
    pass


class MigrationError(Exception):
    """Raised when a legacy state migration cannot complete (e.g. missing artifacts)."""
    pass


class StateStore:
    """Persistent, thread-safe task state store backed by SQLite.

    SQLite is chosen for reliability and concurrent-read / serialized-write.
    The DB file lives under ~/.local/state/supervisor-cao/.
    """

    def __init__(self, db_path: Path | None = None):
        db_path = db_path or DEFAULT_STATE_DIR / "tasks.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = str(db_path)
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    state TEXT NOT NULL,
                    baseline_sha TEXT,
                    candidate_sha TEXT,
                    tested_sha TEXT,
                    reviewed_sha TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    meta TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    detail TEXT,
                    ts REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                )
                """
            )
            c.commit()

    def create(self, task_id: str, project: str, baseline_sha: str | None = None) -> TaskRecord:
        rec = TaskRecord(task_id=task_id, project=project, baseline_sha=baseline_sha)
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO tasks (task_id, project, state, baseline_sha, candidate_sha, tested_sha, reviewed_sha, error, created_at, updated_at, meta) "
                "VALUES (?,?,?,?,NULL,NULL,NULL,NULL,?,?,?)",
                (task_id, project, rec.state, baseline_sha, rec.created_at, rec.updated_at, "{}"),
            )
            self._log_event(c, task_id, "CREATE", None, rec.state, {"project": project})
            c.commit()
        return rec

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        return TaskRecord(
            task_id=row["task_id"], project=row["project"], state=row["state"],
            baseline_sha=row["baseline_sha"], candidate_sha=row["candidate_sha"],
            tested_sha=row["tested_sha"], reviewed_sha=row["reviewed_sha"],
            error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
            meta=json.loads(row["meta"] or "{}"),
        )

    def list(self, project: str | None = None) -> list[TaskRecord]:
        with self._lock, self._conn() as c:
            if project:
                rows = c.execute("SELECT * FROM tasks WHERE project=? ORDER BY updated_at DESC", (project,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
        return [TaskRecord(
            task_id=r["task_id"], project=r["project"], state=r["state"],
            baseline_sha=r["baseline_sha"], candidate_sha=r["candidate_sha"],
            tested_sha=r["tested_sha"], reviewed_sha=r["reviewed_sha"],
            error=r["error"], created_at=r["created_at"], updated_at=r["updated_at"],
            meta=json.loads(r["meta"] or "{}"),
        ) for r in rows]

    def transition(self, task_id: str, to: TaskState | str, *, check_sha: bool = True,
                   new_candidate_sha: str | None = None, tested_sha: str | None = None,
                   reviewed_sha: str | None = None, error: str | None = None,
                   detail: dict | None = None) -> TaskRecord:
        """Transition a task to a new state with full validation.

        Enforces:
        - legal forward transition (or transition to error/terminal)
        - SHA matching: tested_sha == candidate_sha; reviewed_sha == tested_sha
        - any new candidate_sha invalidates tested/reviewed (set to None)
        """
        to_str = to.value if isinstance(to, TaskState) else to
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"task not found: {task_id}")
            from_str = row["state"]
            rec = TaskRecord(
                task_id=row["task_id"], project=row["project"], state=from_str,
                baseline_sha=row["baseline_sha"], candidate_sha=row["candidate_sha"],
                tested_sha=row["tested_sha"], reviewed_sha=row["reviewed_sha"],
                error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"],
                meta=json.loads(row["meta"] or "{}"),
            )

            # Validate transition legality
            if not self._is_legal(from_str, to_str):
                raise IllegalTransition(f"illegal transition {from_str} -> {to_str} for task {task_id}")

            # SHA rules
            if new_candidate_sha is not None and new_candidate_sha != rec.candidate_sha:
                # new commit invalidates old verification and review
                rec.tested_sha = None
                rec.reviewed_sha = None
                rec.candidate_sha = new_candidate_sha

            if tested_sha is not None:
                if rec.candidate_sha is None:
                    raise ShaMismatch("cannot set tested_sha: no candidate_sha")
                if tested_sha != rec.candidate_sha:
                    raise ShaMismatch(f"tested_sha {tested_sha} != candidate_sha {rec.candidate_sha}")
                rec.tested_sha = tested_sha

            if reviewed_sha is not None:
                if rec.tested_sha is None:
                    raise ShaMismatch("cannot set reviewed_sha: no tested_sha")
                if reviewed_sha != rec.tested_sha:
                    raise ShaMismatch(f"reviewed_sha {reviewed_sha} != tested_sha {rec.tested_sha}")
                rec.reviewed_sha = reviewed_sha

            # Gate checks before terminal-success states
            if to_str == TaskState.LOCAL_VERIFIED.value and check_sha:
                if rec.tested_sha is None or rec.tested_sha != rec.candidate_sha:
                    raise ShaMismatch("LOCAL_VERIFIED requires tested_sha == candidate_sha")
            if to_str == TaskState.REMOTE_VERIFIED.value and check_sha:
                if rec.tested_sha is None or rec.tested_sha != rec.candidate_sha:
                    raise ShaMismatch("REMOTE_VERIFIED requires tested_sha == candidate_sha")
            if to_str in (TaskState.APPROVED.value, TaskState.INCREMENTAL_REVIEWING.value) and check_sha:
                if rec.reviewed_sha is None or rec.reviewed_sha != rec.tested_sha:
                    raise ShaMismatch(f"{to_str} requires reviewed_sha == tested_sha")
            if to_str == TaskState.PR_CONTENT_READY.value and check_sha:
                if rec.reviewed_sha is None or rec.reviewed_sha != rec.candidate_sha:
                    raise ShaMismatch("PR_CONTENT_READY requires reviewed_sha == candidate_sha")

            rec.state = to_str
            rec.error = error
            rec.updated_at = time.time()
            c.execute(
                "UPDATE tasks SET state=?, candidate_sha=?, tested_sha=?, reviewed_sha=?, error=?, updated_at=?, meta=? WHERE task_id=?",
                (rec.state, rec.candidate_sha, rec.tested_sha, rec.reviewed_sha, rec.error, rec.updated_at,
                 json.dumps(rec.meta), task_id),
            )
            self._log_event(c, task_id, "TRANSITION", from_str, to_str, detail or {})
            c.commit()
            return rec

    def _is_legal(self, from_str: str, to_str: str) -> bool:
        # error states reachable from any non-terminal state
        if to_str in ERROR_STATES:
            return True
        # FAILED and NEEDS_HUMAN are reachable from any non-terminal state
        # (a stage can fail or need human intervention at any point)
        if to_str in (TaskState.FAILED.value, TaskState.NEEDS_HUMAN.value):
            try:
                src = TaskState(from_str)
            except ValueError:
                return False
            # terminal states cannot transition out
            if src in (TaskState.READY_FOR_HUMAN_REVIEW, TaskState.NEEDS_HUMAN):
                return False
            return True
        try:
            src = TaskState(from_str)
        except ValueError:
            return False
        try:
            dst = TaskState(to_str)
        except ValueError:
            return False
        allowed = TRANSITIONS.get(src, set())
        return dst in allowed

    def _log_event(self, c: sqlite3.Connection, task_id: str, event: str,
                   from_state: str | None, to_state: str | None, detail: dict) -> None:
        c.execute(
            "INSERT INTO events (task_id, event, from_state, to_state, detail, ts) VALUES (?,?,?,?,?,?)",
            (task_id, event, from_state, to_state, json.dumps(detail), time.time()),
        )

    def events(self, task_id: str) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def migrate_legacy_state(self, task_id: str, run_dir: Path,
                             base_branch: str, head_branch: str) -> TaskRecord:
        """Lazily migrate a legacy DRAFT_PR_CREATED task to PR_CONTENT_READY.

        Only called from resume_task/advance_task/dedicated migration — NEVER
        from get_task. If artifacts are incomplete, raises MigrationError (does
        NOT silently roll back to APPROVED, does NOT fake success).
        """
        rec = self.get(task_id)
        if not rec:
            raise KeyError(f"task not found: {task_id}")
        if rec.state != TaskState.DRAFT_PR_CREATED.value:
            return rec  # not legacy, nothing to do
        run_dir = Path(run_dir)
        required = ["plan.json", "implementation.json", "verification.json",
                    "review.json", "codex-budget-summary.json", "push.json"]
        missing = [f for f in required if not (run_dir / f).exists()]
        if missing:
            detail = {"missing": missing, "reason": "legacy_migration_artifacts_incomplete"}
            self.transition(task_id, TaskState.NEEDS_HUMAN,
                            error="LEGACY_MIGRATION_INCOMPLETE", detail=detail)
            raise MigrationError(f"legacy migration incomplete: missing {missing}")
        # Single transaction: generate content package + transition
        from supervisor_cao.pr_content.renderer import render_pr_content
        artifacts = {n: json.loads((run_dir / f).read_text(encoding="utf-8"))
                     for n, f in [("plan", "plan.json"),
                                  ("implementation", "implementation.json"),
                                  ("verification", "verification.json"),
                                  ("review", "review.json"),
                                  ("budget", "codex-budget-summary.json")]}
        push = json.loads((run_dir / "push.json").read_text(encoding="utf-8"))
        json_text, md_text, sha_text = render_pr_content(
            artifacts, task_id, base_branch, head_branch, push)
        (run_dir / "pr-content.json").write_text(json_text, encoding="utf-8")
        (run_dir / "pr-content.md").write_text(md_text, encoding="utf-8")
        (run_dir / "pr-content.sha256").write_text(sha_text, encoding="utf-8")
        with self._lock, self._conn() as c:
            c.execute("UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                      (TaskState.PR_CONTENT_READY.value, time.time(), task_id))
            self._log_event(c, task_id, "LEGACY_STATE_MIGRATED",
                            TaskState.DRAFT_PR_CREATED.value,
                            TaskState.PR_CONTENT_READY.value,
                            {"task_id": task_id})
            c.commit()
        return self.get(task_id)

    def inject_candidate(self, task_id: str, new_sha: str,
                         from_state: TaskState) -> TaskRecord:
        """ACCEPTANCE ONLY: inject a controlled candidate for review-fix testing.

        This is an audited entry point — NOT for production use. It records a
        controlled_candidate_injection event and clears tested/reviewed SHAs
        (the new candidate has not been tested or reviewed yet).
        """
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"task not found: {task_id}")
            from_str = row["state"]
            c.execute(
                "UPDATE tasks SET candidate_sha=?, tested_sha=NULL, reviewed_sha=NULL, "
                "state=?, updated_at=? WHERE task_id=?",
                (new_sha, from_state.value, time.time(), task_id))
            self._log_event(c, task_id, "CONTROLLED_CANDIDATE_INJECTION",
                            from_str, from_state.value,
                            {"new_sha": new_sha, "reason": "acceptance_review_fix"})
            c.commit()
        return self.get(task_id)
