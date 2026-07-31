"""Deterministic long-term Worker monitor (spec §5.2 — progress-based, no total timeout).

This module owns the worker lifecycle: launch, poll, detect stall, reattach, and
report terminal events. The Supervisor LLM is NEVER called for ordinary polling
— it is only invoked on ``COMPLETED / FAILED / STALLED / NEEDS_DECISION`` events.

Design (per the revised plan):

1. **No total timeout.** ``max_runtime`` defaults to ``None``. A worker runs
   indefinitely as long as it shows progress (output growth, CAO status
   ``PROCESSING``, or alive subprocess with CPU/IO change).

2. **Stall detection.** A handle is marked ``STALLED`` only when ALL progress
   indicators are stopped for ``stall_timeout`` seconds (default 1800). Per-stage
   overrides via ``executor_limits.stall_overrides``.

3. **Dual handle model.**
   - ``CaoTerminalHandle``: terminal_id / session_name — for Codex profiles that
     use CAO ``POST /terminals/run-step`` (teardown=False so the terminal survives).
   - ``ProcessHandle``: pid / pgid / stdout_log / stderr_log / exit_code_file —
     for OpenCode profiles launched as a detached subprocess
     (``start_new_session=True`` on POSIX, ``CREATE_NEW_PROCESS_GROUP`` on Windows).
     Ctrl+C kills the foreground orchestrator but NOT the worker subprocess.

4. **Deterministic interface** (not a daemon-thread-only design):
   ``start_worker``, ``poll_worker``, ``wait_for_stage``, ``resume_worker``.
   ``wait_for_stage`` is a synchronous blocking call; the Supervisor calls it once.

5. **Concurrent ownership.** ``workers.db`` records ``owner_id``, ``lease_until``,
   and ``last_heartbeat``. Two supervisors cannot poll or resume the same worker
   simultaneously; the second is rejected until the lease expires.

6. **STALLED is NOT a TaskState.** It is a ``WorkerHandle.status``. The handle
   records ``resume_state`` (the task state to restore after a successful
   reattach). Only when reattach fails does the task transition to
   ``NEEDS_HUMAN``.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from supervisor_cao.mcp.cao_client import CaoClient, WorkerResult

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "supervisor-cao"
DEFAULT_LEASE_DURATION = 300  # seconds; renewed on each poll

# Handle statuses (worker-level, NOT task state)
H_RUNNING = "RUNNING"
H_COMPLETED = "COMPLETED"
H_FAILED = "FAILED"
H_STALLED = "STALLED"

# Polling backoff sequence (seconds): 2, 5, 10, 20, 30, then 30 forever.
POLL_INTERVALS = [2, 5, 10, 20, 30]


@dataclass
class CaoTerminalHandle:
    """Handle for a CAO-backed worker (Codex profiles via run-step)."""
    terminal_id: str
    session_name: str
    provider: str


@dataclass
class ProcessHandle:
    """Handle for a detached-subprocess worker (OpenCode profiles).

    The subprocess is launched in a new session/process group so Ctrl+C on the
    orchestrator does not kill it. stdout/stderr are redirected to log files so
    resume can re-read them. The exit code is written to exit_code_file by a
    reaper thread (or read via os.waitpid on resume).
    """
    pid: int
    pgid: int
    stdout_log: str
    stderr_log: str
    exit_code_file: str


@dataclass
class WorkerHandle:
    """Persisted state of one worker launch."""
    worker_id: str
    task_id: str
    stage: str
    profile: str
    handle_type: str           # "cao_terminal" | "process"
    cao_handle: dict | None    # serialized CaoTerminalHandle
    process_handle: dict | None  # serialized ProcessHandle
    status: str                # RUNNING/COMPLETED/FAILED/STALLED
    output_offset: int         # bytes of output seen so far
    output_hash: str           # hash of recent output for diff
    last_output_at: float      # timestamp of last output change
    last_heartbeat_at: float   # timestamp of last lease renewal
    last_progress_at: float    # timestamp of last progress signal
    exit_code: int | None
    resume_state: str | None   # task state to restore after reattach
    owner_id: str              # UUID of the supervisor that owns this handle
    lease_until: float         # lease expiration timestamp
    started_at: float
    prompt: str = ""           # for re-launch if needed (not used for reattach)
    working_directory: str = ""

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id, "task_id": self.task_id,
            "stage": self.stage, "profile": self.profile,
            "handle_type": self.handle_type,
            "cao_handle": self.cao_handle,
            "process_handle": self.process_handle,
            "status": self.status,
            "output_offset": self.output_offset,
            "output_hash": self.output_hash,
            "last_output_at": self.last_output_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_progress_at": self.last_progress_at,
            "exit_code": self.exit_code,
            "resume_state": self.resume_state,
            "owner_id": self.owner_id,
            "lease_until": self.lease_until,
            "started_at": self.started_at,
            "working_directory": self.working_directory,
        }


@dataclass
class WorkerStatus:
    """Result of one poll_worker call."""
    worker_id: str
    status: str               # RUNNING/COMPLETED/FAILED/STALLED
    last_message: str | None = None
    raw_output: str = ""
    exit_code: int | None = None
    error: str | None = None
    progress_detected: bool = False
    output_grew: bool = False
    terminal_alive: bool = False
    process_alive: bool = False

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id, "status": self.status,
            "last_message": self.last_message,
            "exit_code": self.exit_code, "error": self.error,
            "progress_detected": self.progress_detected,
            "output_grew": self.output_grew,
            "terminal_alive": self.terminal_alive,
            "process_alive": self.process_alive,
        }


class WorkerMonitor:
    """Deterministic worker monitor with dual handle support.

    The monitor persists every handle to SQLite (``workers.db``) so that a
    crashed or Ctrl+C'd orchestrator can resume by calling ``resume_worker``
    with the handle id. Workers keep running across orchestrator restarts.
    """

    def __init__(self, cao_client: CaoClient | None = None,
                 db_path: Path | None = None,
                 run_root: Path | None = None):
        self.cao = cao_client or CaoClient()
        db_path = db_path or DEFAULT_STATE_DIR / "workers.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = str(db_path)
        self._run_root = run_root or (Path.home() / "cao-runs")
        self._lock = threading.Lock()
        self._owner_id = f"{uuid.uuid4()}:{os.getpid()}"  # encode PID for crash detection
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    handle_type TEXT NOT NULL,
                    cao_handle TEXT,
                    process_handle TEXT,
                    status TEXT NOT NULL,
                    output_offset INTEGER NOT NULL DEFAULT 0,
                    output_hash TEXT NOT NULL DEFAULT '',
                    last_output_at REAL NOT NULL,
                    last_heartbeat_at REAL NOT NULL,
                    last_progress_at REAL NOT NULL,
                    exit_code INTEGER,
                    resume_state TEXT,
                    owner_id TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    started_at REAL NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    working_directory TEXT NOT NULL DEFAULT ''
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    ts REAL NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                )
                """
            )
            c.commit()

    # ------------------------------------------------------------------
    # Public deterministic interface
    # ------------------------------------------------------------------

    def start_worker(self, task_id: str, stage: str, profile: str, prompt: str,
                     working_directory: str, session_name: str | None = None,
                     model: str | None = None, timeout: int | None = None,
                     stall_timeout: int = 1800,
                     resume_state: str | None = None) -> str:
        """Launch a worker and return its worker_id. The worker runs in the
        background; call ``wait_for_stage`` or ``poll_worker`` to observe it.

        - Codex profiles (codex-planner/reviewer/judge) use CAO run-step with
          ``teardown=False``; the terminal survives for reattach.
        - OpenCode profiles (researcher/glm-executor/qwen-verifier) launch as a
          detached subprocess in a new session/process group.

        ``timeout=None`` means no total time limit — the worker is allowed to
        run indefinitely as long as it shows progress.
        """
        worker_id = str(uuid.uuid4())
        provider = self.cao.provider_for(profile)
        run_dir = self._run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)

        if provider == "codex":
            handle = self._start_cao_worker(
                worker_id, task_id, stage, profile, prompt,
                working_directory, session_name, model, timeout, run_dir)
            handle_type = "cao_terminal"
            cao_handle = {
                "terminal_id": handle.terminal_id,
                "session_name": handle.session_name,
                "provider": handle.provider,
            }
            proc_handle = None
        else:
            handle = self._start_process_worker(
                worker_id, task_id, stage, profile, prompt,
                working_directory, session_name, model, timeout, run_dir)
            handle_type = "process"
            cao_handle = None
            proc_handle = {
                "pid": handle.pid, "pgid": handle.pgid,
                "stdout_log": handle.stdout_log,
                "stderr_log": handle.stderr_log,
                "exit_code_file": handle.exit_code_file,
            }

        now = time.time()
        wh = WorkerHandle(
            worker_id=worker_id, task_id=task_id, stage=stage, profile=profile,
            handle_type=handle_type, cao_handle=cao_handle,
            process_handle=proc_handle, status=H_RUNNING,
            output_offset=0, output_hash="",
            last_output_at=now, last_heartbeat_at=now, last_progress_at=now,
            exit_code=None, resume_state=resume_state,
            owner_id=self._owner_id, lease_until=now + DEFAULT_LEASE_DURATION,
            started_at=now, prompt=prompt, working_directory=working_directory,
        )
        self._persist(wh)
        self._log_event(worker_id, "STARTED", {"handle_type": handle_type})
        return worker_id

    def poll_worker(self, worker_id: str) -> WorkerStatus:
        """Read the current status of a worker (one poll iteration).

        Does NOT block. Returns the current status and progress indicators.
        Updates the lease and heartbeat on the handle.
        """
        wh = self._load(worker_id)
        if wh is None:
            return WorkerStatus(worker_id, H_FAILED, error="handle not found")
        if not self._check_and_renew_ownership(wh):
            return WorkerStatus(worker_id, H_FAILED,
                                error="ownership lost (another supervisor owns this handle)")

        if wh.status in (H_COMPLETED, H_FAILED):
            # terminal — return cached result
            return WorkerStatus(worker_id, wh.status, exit_code=wh.exit_code)

        # If already STALLED, don't poll further (caller should resume_worker)
        if wh.status == H_STALLED:
            return WorkerStatus(worker_id, H_STALLED, error="handle stalled; call resume_worker")

        status = self._do_poll(wh)
        # update handle with poll results
        self._update_from_poll(wh, status)
        return status

    def wait_for_stage(self, task_id: str, stall_timeout: int = 1800,
                       max_polls: int | None = None) -> dict:
        """Block until the current stage's worker reaches a terminal state
        (COMPLETED / FAILED / STALLED). Returns the final WorkerStatus as dict.

        This is the single call the Supervisor makes — ordinary polling happens
        inside this method, not via the LLM. Poll intervals back off from 2s
        to 30s.

        ``max_polls=None`` means no poll limit (wait indefinitely as long as
        the worker shows progress).
        """
        # find the RUNNING worker for this task
        wh = self._find_running_for_task(task_id)
        if wh is None:
            return {"worker_id": None, "status": H_FAILED,
                    "error": "no running worker for task"}
        stall_deadline = time.time() + stall_timeout
        poll_idx = 0
        last_progress = wh.last_progress_at
        poll_count = 0
        while True:
            if max_polls is not None and poll_count >= max_polls:
                return {"worker_id": wh.worker_id, "status": H_FAILED,
                        "error": "max_polls exceeded"}
            poll_count += 1
            status = self.poll_worker(wh.worker_id)
            if status.progress_detected:
                last_progress = time.time()
                stall_deadline = time.time() + stall_timeout
                poll_idx = 0  # reset backoff on progress
            if status.status in (H_COMPLETED, H_FAILED):
                return status.to_dict()
            if status.status == H_STALLED:
                # attempt reattach once
                if self.resume_worker(wh.worker_id):
                    stall_deadline = time.time() + stall_timeout
                    poll_idx = 0
                    continue
                # reattach failed
                return status.to_dict()
            # still running: check stall
            if time.time() - last_progress > stall_timeout:
                self._mark_stalled(wh.worker_id)
                return WorkerStatus(wh.worker_id, H_STALLED,
                                    error=f"stalled: no progress for {stall_timeout}s").to_dict()
            interval = POLL_INTERVALS[min(poll_idx, len(POLL_INTERVALS) - 1)]
            poll_idx += 1
            time.sleep(interval)

    def resume_worker(self, worker_id: str) -> bool:
        """Reattach to a STALLED or orphaned worker handle.

        - CAO terminal: re-read ``GET /terminals/{id}/status``; if the terminal
          is still alive (status != error and HTTP 200), mark RUNNING again.
        - Process: check ``os.kill(pid, 0)``; if alive, mark RUNNING again.
        Returns True if reattach succeeded, False if the worker is gone.
        """
        wh = self._load(worker_id)
        if wh is None:
            return False
        # claim ownership for resume
        if not self._claim_ownership(wh):
            return False
        if wh.handle_type == "cao_terminal" and wh.cao_handle:
            tid = wh.cao_handle["terminal_id"]
            ts = self.cao.get_terminal_status(tid)
            alive = ts.get("status") not in ("error", "unknown", None)
            if not alive:
                self._log_event(worker_id, "REATtach_FAILED",
                                {"reason": f"terminal status={ts.get('status')}"})
                return False
        elif wh.handle_type == "process" and wh.process_handle:
            pid = wh.process_handle["pid"]
            if not self._process_alive(pid):
                # check exit code file
                self._read_exit_code(wh)
                self._log_event(worker_id, "REATtach_FAILED",
                                {"reason": "process not alive"})
                return False
        else:
            return False
        # reattach succeeded: restore RUNNING
        now = time.time()
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET status=?, last_progress_at=?, last_heartbeat_at=?, "
                "lease_until=? WHERE worker_id=?",
                (H_RUNNING, now, now, now + DEFAULT_LEASE_DURATION, worker_id),
            )
            c.commit()
        self._log_event(worker_id, "REATTACHED", {})
        return True

    def get_handle(self, worker_id: str) -> WorkerHandle | None:
        return self._load(worker_id)

    def find_for_task(self, task_id: str) -> WorkerHandle | None:
        """Find the most recent worker handle for a task."""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM workers WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_handle(row) if row else None

    def peek_worker(self, worker_id: str) -> WorkerStatus:
        """Read-only poll: check worker status WITHOUT acquiring/renewing lease.

        Used by task watch / logs --follow (read-only commands that must not
        interfere with an active Controller's ownership).
        """
        wh = self._load(worker_id)
        if wh is None:
            return WorkerStatus(worker_id, H_FAILED, error="handle not found")
        if wh.status in (H_COMPLETED, H_FAILED):
            return WorkerStatus(worker_id, wh.status, exit_code=wh.exit_code)
        if wh.status == H_STALLED:
            return WorkerStatus(worker_id, H_STALLED, error="handle stalled")
        # Do a read-only poll (no lease update)
        status = self._do_poll(wh)
        # Do NOT call _update_from_poll (that renews lease). Just return status.
        return status

    def release_ownership(self, worker_id: str) -> None:
        """Release the Controller's lease on a worker (for Ctrl+C exit).

        Does NOT kill the worker. The worker continues running in the background.
        """
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET lease_until=? WHERE worker_id=? AND owner_id=?",
                (time.time(), worker_id, self._owner_id),
            )
            c.commit()
        self._log_event(worker_id, "LEASE_RELEASED", {"controller": self._owner_id})

    def safe_takeover(self, worker_id: str) -> bool:
        """Safely take over an orphaned worker handle after a Controller crash.

        Checks if the previous owner_pid is still alive. If alive and lease
        valid → refuse (two active Controllers must not own the same handle).
        If dead or lease expired → claim ownership.
        """
        wh = self._load(worker_id)
        if wh is None:
            return False
        now = time.time()
        # Check previous owner_pid (stored in owner_id field as "uuid:pid")
        prev_owner_pid = self._extract_owner_pid(wh.owner_id)
        if prev_owner_pid and self._process_alive(prev_owner_pid) and now < wh.lease_until:
            # Previous owner still alive and lease valid — refuse
            self._log_event(worker_id, "TAKEOVER_REFUSED",
                            {"reason": "owner still alive", "owner_pid": prev_owner_pid})
            return False
        # Claim ownership
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET owner_id=?, lease_until=?, last_heartbeat_at=? "
                "WHERE worker_id=?",
                (self._owner_id, now + DEFAULT_LEASE_DURATION, now, worker_id),
            )
            c.commit()
        self._log_event(worker_id, "TAKEOVER", {"new_owner": self._owner_id})
        return True

    @staticmethod
    def _extract_owner_pid(owner_id: str) -> int | None:
        """Extract PID from owner_id if encoded as 'uuid:pid'."""
        if ":" in owner_id:
            try:
                return int(owner_id.rsplit(":", 1)[1])
            except (ValueError, IndexError):
                pass
        return None

    # ------------------------------------------------------------------
    # Internal: launch helpers
    # ------------------------------------------------------------------

    def _start_cao_worker(self, worker_id, task_id, stage, profile, prompt,
                          working_directory, session_name, model, timeout,
                          run_dir) -> CaoTerminalHandle:
        """Launch a Codex worker via CAO run-step (teardown=False, non-blocking).

        We send the run-step request with a long timeout but do NOT wait for
        completion here — instead we save the terminal_id and let poll_worker
        read the status. However, CAO's run-step is synchronous (it blocks
        until the step completes), so we launch it in a background thread and
        capture the terminal_id from the initial response.

        For Codex, the run-step response includes terminal_id even on timeout,
        so we use a short initial probe to get the terminal_id, then poll.
        """
        # CAO run-step is blocking; we launch it in a thread and use the
        # terminal_id from the first response (or the error response which
        # also includes terminal_id). The thread writes the final result to
        # a file that poll_worker reads.
        t = timeout  # may be None → use a very large int for the HTTP call
        http_timeout = (t if t else 3600) + 180.0
        result_file = run_dir / f"{stage}-cao-result.json"
        thread = threading.Thread(
            target=self._cao_worker_thread,
            args=(worker_id, task_id, stage, profile, prompt,
                  working_directory, session_name, model, t, result_file),
            daemon=True,
        )
        thread.start()
        # Wait briefly for the terminal_id to appear (CAO creates it quickly).
        # The thread writes a partial result file with terminal_id before
        # blocking on the HTTP call.
        deadline = time.time() + 30
        terminal_id = None
        while time.time() < deadline:
            if result_file.exists():
                try:
                    partial = json.loads(result_file.read_text())
                    terminal_id = partial.get("terminal_id")
                    if terminal_id:
                        break
                except Exception:
                    pass
            time.sleep(0.5)
        if not terminal_id:
            # The thread may have failed to start or CAO is slow. Record a
            # placeholder; poll_worker will detect the failure.
            terminal_id = f"unknown-{worker_id[:8]}"
        return CaoTerminalHandle(
            terminal_id=terminal_id,
            session_name=session_name or f"scao-{task_id}",
            provider="codex",
        )

    def _cao_worker_thread(self, worker_id, task_id, stage, profile, prompt,
                            working_directory, session_name, model, timeout,
                            result_file):
        """Background thread that calls CAO run-step and writes the result."""
        try:
            # Write partial result with a marker so the launcher can read
            # terminal_id early (we don't have it yet, so we'll rely on the
            # error/partial response). For a true non-blocking design, CAO
            # would need an async run-step; for now we use the blocking call
            # and rely on terminal_id from the error response on timeout.
            result = self.cao.launch_worker(
                profile, prompt, working_directory, session_name, model,
                timeout, task_id=task_id, stage=stage)
            data = result.to_dict()
            data["last_message"] = result.last_message
            data["raw_output"] = result.raw_output
            data["success"] = result.success
            data["terminal_id"] = result.terminal_id
            data["done"] = True
            result_file.write_text(json.dumps(data, default=str))
        except Exception as e:
            result_file.write_text(json.dumps(
                {"done": True, "success": False, "error": str(e), "terminal_id": None}))

    def _start_process_worker(self, worker_id, task_id, stage, profile, prompt,
                              working_directory, session_name, model, timeout,
                              run_dir) -> ProcessHandle:
        """Launch an OpenCode worker via worker-shim (persistent, no daemon reaper).

        Uses ``scripts/worker-shim`` to wrap the OpenCode command in an
        independent process that survives Controller exit. The shim writes
        stdout/stderr to log files, result.json, and exit-code — all persistent.
        No daemon reaper thread is needed; poll_worker reads these files.
        """
        model_arg = model or _profile_model(profile)
        cmd = ["opencode", "run", "--format", "json", "--agent", profile]
        if model_arg:
            cmd += ["-m", model_arg]
        cmd.append(prompt)
        stdout_log = str(run_dir / f"{stage}-stdout.log")
        stderr_log = str(run_dir / f"{stage}-stderr.log")
        exit_code_file = str(run_dir / f"{stage}-exit.txt")
        result_file = str(run_dir / f"{stage}-result.json")
        # Launch via worker-shim (persistent, survives Controller exit)
        shim = Path(__file__).resolve().parents[3] / "scripts" / "worker-shim"
        shim_cmd = [
            sys.executable, str(shim),
            "--stdout", stdout_log, "--stderr", stderr_log,
            "--result", result_file, "--exit-code", exit_code_file,
            "--", *cmd,
        ]
        kwargs: dict[str, Any] = {"cwd": working_directory}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(shim_cmd, **kwargs)
        pid = proc.pid
        pgid = pid
        if sys.platform != "win32":
            try:
                pgid = os.getpgid(pid)
            except Exception:
                pass
        # Do NOT start a daemon reaper thread. The shim writes exit-code and
        # result.json itself. poll_worker reads those files.
        self._log_event(worker_id, "PROCESS_STARTED", {"pid": pid, "pgid": pgid})
        return ProcessHandle(
            pid=pid, pgid=pgid,
            stdout_log=stdout_log, stderr_log=stderr_log,
            exit_code_file=exit_code_file,
        )

    # ------------------------------------------------------------------
    # Internal: polling
    # ------------------------------------------------------------------

    def _do_poll(self, wh: WorkerHandle) -> WorkerStatus:
        """Perform one poll iteration based on handle type."""
        if wh.handle_type == "cao_terminal" and wh.cao_handle:
            return self._poll_cao(wh)
        if wh.handle_type == "process" and wh.process_handle:
            return self._poll_process(wh)
        return WorkerStatus(wh.worker_id, H_FAILED, error="unknown handle type")

    def _poll_cao(self, wh: WorkerHandle) -> WorkerStatus:
        tid = wh.cao_handle["terminal_id"]
        if tid.startswith("unknown-"):
            # terminal_id not yet known; check result file
            return self._check_cao_result_file(wh)
        ts = self.cao.get_terminal_status(tid)
        status_str = ts.get("status", "unknown")
        # Check for result file (the background thread writes it on completion)
        result_status = self._check_cao_result_file(wh)
        if result_status.status == H_COMPLETED:
            return result_status
        # Read output for progress detection
        output = self.cao.get_terminal_output(tid, mode="full")
        output_grew = len(output) > wh.output_offset
        new_hash = hashlib.md5(output[-2000:].encode()).hexdigest()
        hash_changed = new_hash != wh.output_hash
        terminal_alive = status_str not in ("error", "unknown", None)
        processing = status_str == "processing"
        progress = output_grew or hash_changed or processing
        if status_str in ("completed", "idle", "waiting_user_answer"):
            # terminal done; try to extract last_message from result file
            return self._check_cao_result_file(wh, force_complete=True)
        if status_str == "error":
            return WorkerStatus(wh.worker_id, H_FAILED,
                                error=f"terminal error: {ts.get('error', '')}",
                                terminal_alive=False)
        return WorkerStatus(wh.worker_id, H_RUNNING,
                            progress_detected=progress,
                            output_grew=output_grew,
                            terminal_alive=terminal_alive)

    def _check_cao_result_file(self, wh: WorkerHandle,
                               force_complete: bool = False) -> WorkerStatus:
        """Check if the background CAO thread has written the result file."""
        run_dir = self._run_root / wh.task_id
        result_file = run_dir / f"{wh.stage}-cao-result.json"
        if not result_file.exists():
            return WorkerStatus(wh.worker_id, H_RUNNING,
                                progress_detected=False)
        try:
            data = json.loads(result_file.read_text())
        except Exception:
            return WorkerStatus(wh.worker_id, H_RUNNING)
        if not data.get("done") and not force_complete:
            return WorkerStatus(wh.worker_id, H_RUNNING)
        success = data.get("success", False)
        last_message = data.get("last_message")
        raw = data.get("raw_output", "")
        if success and last_message:
            return WorkerStatus(wh.worker_id, H_COMPLETED,
                                last_message=last_message, raw_output=raw,
                                exit_code=0, progress_detected=True)
        return WorkerStatus(wh.worker_id, H_FAILED,
                            error=data.get("error", "cao worker failed"),
                            raw_output=raw)

    def _poll_process(self, wh: WorkerHandle) -> WorkerStatus:
        """Poll a process worker. Reads result.json/exit-code files (no daemon reaper).

        Progress detection:
          - output grew (stdout log size increased)
          - output hash changed
          - process still alive (liveness, not progress)
          - CPU time changed (best-effort, POSIX only)
          - child subprocesses still running (best-effort)
        """
        ph = wh.process_handle
        pid = ph["pid"]
        alive = self._process_alive(pid)
        # Read output for progress detection
        stdout_log = ph["stdout_log"]
        output_grew = False
        output = ""
        try:
            if Path(stdout_log).exists():
                content = Path(stdout_log).read_bytes()
                output = content.decode("utf-8", errors="replace")
                output_grew = len(content) > wh.output_offset
        except Exception:
            pass
        new_hash = hashlib.md5(output[-2000:].encode()).hexdigest() if output else ""
        hash_changed = new_hash != wh.output_hash
        # CPU time change detection (POSIX only, best-effort)
        cpu_changed = self._cpu_time_changed(pid, wh)
        # Child subprocess running (best-effort)
        children_running = self._children_running(pid, ph.get("pgid", pid))
        # Check result.json (written by worker-shim on completion)
        result_file = self._run_root / wh.task_id / f"{wh.stage}-result.json"
        if result_file.exists():
            try:
                data = json.loads(result_file.read_text())
                if data.get("done"):
                    exit_code = data.get("exit_code", -1)
                    last_message = data.get("last_message")
                    raw = data.get("raw_output", output)
                    if not last_message:
                        last_message = _extract_opencode_message(raw)
                    if exit_code == 0 and last_message:
                        return WorkerStatus(wh.worker_id, H_COMPLETED,
                                            last_message=last_message, raw_output=raw,
                                            exit_code=exit_code, progress_detected=True)
                    return WorkerStatus(wh.worker_id, H_FAILED,
                                        error=f"process exited code={exit_code}",
                                        exit_code=exit_code, raw_output=raw)
            except Exception:
                pass
        # Fallback: check exit code file
        exit_code = self._read_exit_code_val(ph["exit_code_file"])
        if exit_code is not None:
            last_message = _extract_opencode_message(output)
            if exit_code == 0 and last_message:
                return WorkerStatus(wh.worker_id, H_COMPLETED,
                                    last_message=last_message, raw_output=output,
                                    exit_code=exit_code, progress_detected=True)
            return WorkerStatus(wh.worker_id, H_FAILED,
                                error=f"process exited code={exit_code}",
                                exit_code=exit_code, raw_output=output)
        # Progress: output grew OR hash changed OR CPU changed OR children running.
        # PROCESSING (alive) is liveness, NOT progress.
        progress = output_grew or hash_changed or cpu_changed or children_running
        return WorkerStatus(wh.worker_id, H_RUNNING,
                            progress_detected=progress,
                            output_grew=output_grew,
                            process_alive=alive)

    # ------------------------------------------------------------------
    # Internal: stall + ownership
    # ------------------------------------------------------------------

    def _update_from_poll(self, wh: WorkerHandle, status: WorkerStatus):
        """Update the handle in DB with poll results."""
        now = time.time()
        new_offset = wh.output_offset
        new_hash = wh.output_hash
        if status.output_grew:
            # re-read to get accurate length (best-effort)
            new_offset = wh.output_offset + 1  # approximate; exact length not needed
            new_hash = hashlib.md5(status.raw_output[-2000:].encode()).hexdigest() if status.raw_output else new_hash
        progress = status.progress_detected
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET status=?, output_offset=?, output_hash=?, "
                "last_output_at=?, last_heartbeat_at=?, last_progress_at=?, "
                "lease_until=?, exit_code=? WHERE worker_id=?",
                (status.status if status.status != H_RUNNING else H_RUNNING,
                 new_offset, new_hash,
                 now if status.output_grew else wh.last_output_at,
                 now,
                 now if progress else wh.last_progress_at,
                 now + DEFAULT_LEASE_DURATION,
                 status.exit_code,
                 wh.worker_id),
            )
            c.commit()
        # log state transitions
        if status.status != H_RUNNING and status.status != wh.status:
            self._log_event(wh.worker_id, status.status, status.to_dict())

    def _mark_stalled(self, worker_id: str):
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET status=? WHERE worker_id=?",
                (H_STALLED, worker_id),
            )
            c.commit()
        self._log_event(worker_id, H_STALLED, {})

    def _check_and_renew_ownership(self, wh: WorkerHandle) -> bool:
        """Check if we still own this handle; renew the lease. If another
        supervisor owns it (lease not expired), return False."""
        now = time.time()
        if wh.owner_id != self._owner_id and now < wh.lease_until:
            return False  # another owner, lease valid
        # claim or renew
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET owner_id=?, lease_until=?, last_heartbeat_at=? "
                "WHERE worker_id=?",
                (self._owner_id, now + DEFAULT_LEASE_DURATION, now, wh.worker_id),
            )
            c.commit()
        return True

    def _claim_ownership(self, wh: WorkerHandle) -> bool:
        """Claim ownership for resume. Allowed if the lease has expired or
        we already own it."""
        now = time.time()
        if wh.owner_id == self._owner_id:
            return True
        if now < wh.lease_until:
            return False  # another owner, lease still valid
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE workers SET owner_id=?, lease_until=?, last_heartbeat_at=? "
                "WHERE worker_id=?",
                (self._owner_id, now + DEFAULT_LEASE_DURATION, now, wh.worker_id),
            )
            c.commit()
        return True

    # ------------------------------------------------------------------
    # Internal: persistence
    # ------------------------------------------------------------------

    def _persist(self, wh: WorkerHandle):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO workers (worker_id, task_id, stage, profile, handle_type, "
                "cao_handle, process_handle, status, output_offset, output_hash, "
                "last_output_at, last_heartbeat_at, last_progress_at, exit_code, "
                "resume_state, owner_id, lease_until, started_at, prompt, working_directory) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wh.worker_id, wh.task_id, wh.stage, wh.profile, wh.handle_type,
                 json.dumps(wh.cao_handle) if wh.cao_handle else None,
                 json.dumps(wh.process_handle) if wh.process_handle else None,
                 wh.status, wh.output_offset, wh.output_hash,
                 wh.last_output_at, wh.last_heartbeat_at, wh.last_progress_at,
                 wh.exit_code, wh.resume_state,
                 wh.owner_id, wh.lease_until, wh.started_at,
                 wh.prompt, wh.working_directory),
            )
            c.commit()

    def _load(self, worker_id: str) -> WorkerHandle | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,),
            ).fetchone()
        return self._row_to_handle(row) if row else None

    def _find_running_for_task(self, task_id: str) -> WorkerHandle | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM workers WHERE task_id=? AND status=? "
                "ORDER BY started_at DESC LIMIT 1",
                (task_id, H_RUNNING),
            ).fetchone()
        if not row:
            row = c.execute(  # type: ignore
                "SELECT * FROM workers WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return self._row_to_handle(row) if row else None

    def _row_to_handle(self, row: sqlite3.Row) -> WorkerHandle:
        cao = json.loads(row["cao_handle"]) if row["cao_handle"] else None
        proc = json.loads(row["process_handle"]) if row["process_handle"] else None
        return WorkerHandle(
            worker_id=row["worker_id"], task_id=row["task_id"],
            stage=row["stage"], profile=row["profile"],
            handle_type=row["handle_type"],
            cao_handle=cao, process_handle=proc,
            status=row["status"],
            output_offset=row["output_offset"],
            output_hash=row["output_hash"],
            last_output_at=row["last_output_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            last_progress_at=row["last_progress_at"],
            exit_code=row["exit_code"],
            resume_state=row["resume_state"],
            owner_id=row["owner_id"],
            lease_until=row["lease_until"],
            started_at=row["started_at"],
            prompt=row["prompt"],
            working_directory=row["working_directory"],
        )

    def _log_event(self, worker_id: str, event: str, detail: dict):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO worker_events (worker_id, event, detail, ts) VALUES (?,?,?,?)",
                (worker_id, event, json.dumps(detail, default=str), time.time()),
            )
            c.commit()

    def list_events(self, worker_id: str) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM worker_events WHERE worker_id=? ORDER BY id",
                (worker_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Internal: process helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore
                handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED
                if not handle:
                    return False
                kernel32.CloseHandle(handle)
                return True
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    @staticmethod
    def _cpu_time_changed(pid: int, wh: WorkerHandle) -> bool:
        """Check if CPU time changed since last poll (POSIX, best-effort)."""
        if sys.platform == "win32":
            return False  # not easily available on Windows
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().split()
            utime = int(stat[13])
            stime = int(stat[14])
            total = utime + stime
            prev = getattr(wh, '_prev_cpu_time', None)
            if prev is not None and total != prev:
                wh._prev_cpu_time = total  # type: ignore
                return True
            wh._prev_cpu_time = total  # type: ignore
        except Exception:
            pass
        return False

    @staticmethod
    def _children_running(pid: int, pgid: int) -> bool:
        """Check if any child subprocess is running (best-effort, POSIX)."""
        if sys.platform == "win32":
            return False
        try:
            # Check /proc for children of this pid
            proc_children = Path(f"/proc/{pid}/task/{pid}/children")
            if proc_children.exists():
                children = proc_children.read_text().strip().split()
                if children:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _read_exit_code_val(exit_code_file: str) -> int | None:
        try:
            p = Path(exit_code_file)
            if p.exists():
                return int(p.read_text().strip())
        except Exception:
            pass
        return None

    def _read_exit_code(self, wh: WorkerHandle):
        """Read exit code from file and update handle."""
        if wh.process_handle:
            code = self._read_exit_code_val(wh.process_handle["exit_code_file"])
            if code is not None:
                with self._lock, self._conn() as c:
                    c.execute(
                        "UPDATE workers SET exit_code=? WHERE worker_id=?",
                        (code, wh.worker_id),
                    )
                    c.commit()


def _profile_model(profile: str) -> str | None:
    """Resolve the model id for a profile from local config."""
    from supervisor_cao.projects.model_resolver import resolve_model
    return resolve_model(profile)


def _extract_opencode_message(stdout: str) -> str | None:
    """Extract the last assistant message from opencode run --format json output."""
    if not stdout:
        return None
    messages: dict[str, list[str]] = {}
    order: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "text":
            continue
        part = ev.get("part", {})
        if part.get("type") != "text":
            continue
        msg_id = part.get("messageID") or ev.get("messageID") or "_"
        if msg_id not in messages:
            messages[msg_id] = []
            order.append(msg_id)
        messages[msg_id].append(part.get("text", ""))
    if not order:
        return None
    text = "".join(messages[order[-1]]).strip()
    return text if text else None
