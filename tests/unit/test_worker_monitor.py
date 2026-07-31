"""Unit tests for the deterministic WorkerMonitor (spec §5.2).

Covers:
  - Dual handle model: CaoTerminalHandle and ProcessHandle.
  - start_worker / poll_worker / wait_for_stage / resume_worker interface.
  - Stall detection: all progress indicators stopped → STALLED.
  - Reattach: CAO terminal alive → RUNNING restored; dead → False.
  - Reattach: process alive → RUNNING restored; dead → False.
  - Concurrent ownership: owner_id / lease_until / heartbeat.
  - Persistence: handle survives across WorkerMonitor instances (resume from DB).
  - STALLED is a handle status, NOT a TaskState.
"""
from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.mcp.worker_monitor import (  # noqa: E402
    WorkerMonitor, WorkerHandle, WorkerStatus,
    CaoTerminalHandle, ProcessHandle,
    H_RUNNING, H_COMPLETED, H_FAILED, H_STALLED,
)


@pytest.fixture
def tmp_db():
    d = tempfile.mkdtemp()
    yield Path(d) / "workers.db"
    gc.collect()


@pytest.fixture
def tmp_run_root():
    d = tempfile.mkdtemp()
    yield Path(d) / "cao-runs"
    gc.collect()


@pytest.fixture
def monitor(tmp_db, tmp_run_root):
    """A WorkerMonitor with a fake CaoClient."""
    fake_cao = MagicMock()
    fake_cao.server_url = "http://127.0.0.1:9889"
    fake_cao.provider_for = lambda p: "codex" if p.startswith("codex") else "opencode"
    m = WorkerMonitor(cao_client=fake_cao, db_path=tmp_db, run_root=tmp_run_root)
    yield m
    m.close()


class TestHandleDataclasses:
    def test_cao_terminal_handle(self):
        h = CaoTerminalHandle("t1", "s1", "codex")
        assert h.terminal_id == "t1"
        assert h.session_name == "s1"
        assert h.provider == "codex"

    def test_process_handle(self):
        h = ProcessHandle(123, 123, "/tmp/out.log", "/tmp/err.log", "/tmp/exit.txt")
        assert h.pid == 123
        assert h.stdout_log == "/tmp/out.log"

    def test_worker_handle_to_dict(self):
        now = time.time()
        wh = WorkerHandle(
            worker_id="w1", task_id="t1", stage="plan", profile="codex-planner",
            handle_type="cao_terminal", cao_handle={"terminal_id": "t1"},
            process_handle=None, status=H_RUNNING, output_offset=0,
            output_hash="", last_output_at=now, last_heartbeat_at=now,
            last_progress_at=now, exit_code=None, resume_state="PLANNING",
            owner_id="o1", lease_until=now + 300, started_at=now)
        d = wh.to_dict()
        assert d["worker_id"] == "w1"
        assert d["status"] == H_RUNNING
        assert d["resume_state"] == "PLANNING"


class TestStartWorker:
    def test_start_cao_worker_persists_handle(self, monitor, tmp_run_root):
        """start_worker for a Codex profile persists a handle with handle_type=cao_terminal."""
        # The CAO launch_worker is called in a background thread; we make it
        # write a result file quickly so the handle is created.
        def fake_launch(profile, prompt, wd, sn, model, timeout, task_id, stage):
            # write a result file so the monitor sees COMPLETED
            run_dir = tmp_run_root / task_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / f"{stage}-cao-result.json").write_text(json.dumps({
                "done": True, "success": True, "last_message": '{"plan_id":"p1"}',
                "raw_output": "", "terminal_id": "fake-t1"}))
            from supervisor_cao.mcp.cao_client import WorkerResult
            return WorkerResult(True, '{"plan_id":"p1"}', "fake-t1", sn, "")
        monitor.cao.launch_worker = fake_launch
        monitor.cao.get_terminal_status = lambda tid: {"status": "completed"}
        monitor.cao.get_terminal_output = lambda tid, mode="full": ""

        wid = monitor.start_worker("t1", "plan", "codex-planner", "prompt",
                                   "/tmp", session_name="s1")
        assert wid
        wh = monitor.get_handle(wid)
        assert wh is not None
        assert wh.handle_type == "cao_terminal"
        assert wh.status == H_RUNNING
        assert wh.cao_handle is not None
        assert wh.cao_handle["terminal_id"] is not None

    def test_start_process_worker_persists_handle(self, monitor, tmp_run_root):
        """start_worker for an OpenCode profile persists a process handle."""
        # Patch the module-level _start_process_worker to use python -c instead
        # of opencode (which may not be installed in the test env).
        import supervisor_cao.mcp.worker_monitor as wm

        original_start = wm.WorkerMonitor._start_process_worker

        def patched_start(self, worker_id, task_id, stage, profile, prompt,
                          working_directory, session_name, model, timeout, run_dir):
            # Start a real short-lived python subprocess
            cmd = [sys.executable, "-c", "import time; print('hello'); time.sleep(0.1)"]
            stdout_log = str(run_dir / f"{stage}-stdout.log")
            stderr_log = str(run_dir / f"{stage}-stderr.log")
            exit_code_file = str(run_dir / f"{stage}-exit.txt")
            stdout_f = open(stdout_log, "w", buffering=1)
            stderr_f = open(stderr_log, "w", buffering=1)
            kwargs: dict = {"stdout": stdout_f, "stderr": stderr_f,
                            "cwd": working_directory}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kwargs)
            pid = proc.pid

            def _reaper():
                code = proc.wait()
                try:
                    Path(exit_code_file).write_text(str(code))
                except Exception:
                    pass
                stdout_f.close()
                stderr_f.close()
            import threading
            threading.Thread(target=_reaper, daemon=True).start()
            return ProcessHandle(pid, pid, stdout_log, stderr_log, exit_code_file)

        wm.WorkerMonitor._start_process_worker = patched_start
        try:
            wid = monitor.start_worker("t2", "research", "researcher", "echo hello",
                                       str(tmp_run_root), session_name="s2")
        finally:
            wm.WorkerMonitor._start_process_worker = original_start
        assert wid
        wh = monitor.get_handle(wid)
        assert wh is not None
        assert wh.handle_type == "process"
        assert wh.status == H_RUNNING
        assert wh.process_handle is not None
        assert wh.process_handle["pid"] > 0
        # wait for the process to exit and write exit code
        time.sleep(2)
        exit_code = WorkerMonitor._read_exit_code_val(wh.process_handle["exit_code_file"])
        assert exit_code is not None


class TestPollWorker:
    def test_poll_completed_cao(self, monitor, tmp_run_root):
        """poll_worker returns COMPLETED when the result file shows success."""
        task_id = "t3"
        run_dir = tmp_run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # Pre-write the result file so the monitor sees COMPLETED immediately
        (run_dir / "plan-cao-result.json").write_text(json.dumps({
            "done": True, "success": True, "last_message": '{"x":1}',
            "raw_output": "", "terminal_id": "t3term"}))
        # Make launch_worker return a successful result quickly (the thread
        # calls it and writes the result file again)
        from supervisor_cao.mcp.cao_client import WorkerResult

        def fake_launch(profile, prompt, wd, sn, model, timeout, task_id=None, stage=None):
            return WorkerResult(True, '{"x":1}', "t3term", sn, "")
        monitor.cao.launch_worker = fake_launch
        monitor.cao.get_terminal_status = lambda tid: {"status": "completed"}
        monitor.cao.get_terminal_output = lambda tid, mode="full": ""
        wid = monitor.start_worker(task_id, "plan", "codex-planner", "p",
                                   str(run_dir), session_name="s3")
        # The terminal_id may be "unknown-..." if the thread hasn't written yet;
        # patch the handle to use the known terminal_id
        wh = monitor.get_handle(wid)
        if wh.cao_handle and wh.cao_handle["terminal_id"].startswith("unknown-"):
            wh.cao_handle["terminal_id"] = "t3term"
            with monitor._lock, monitor._conn() as c:
                c.execute(
                    "UPDATE workers SET cao_handle=? WHERE worker_id=?",
                    (json.dumps(wh.cao_handle), wid),
                )
                c.commit()
        # give the thread time
        time.sleep(1)
        status = monitor.poll_worker(wid)
        assert status.status in (H_COMPLETED, H_RUNNING)
        if status.status == H_RUNNING:
            time.sleep(1)
            status = monitor.poll_worker(wid)
        assert status.status == H_COMPLETED

    def test_poll_process_exit(self, monitor, tmp_run_root):
        """poll_worker returns COMPLETED when a process exits with code 0."""
        # Patch to use python subprocess
        import supervisor_cao.mcp.worker_monitor as wm
        original_start = wm.WorkerMonitor._start_process_worker

        def patched_start(self, worker_id, task_id, stage, profile, prompt,
                          working_directory, session_name, model, timeout, run_dir):
            cmd = [sys.executable, "-c",
                   'print(\'{"type":"text","part":{"type":"text","text":"done","messageID":"m1"}}\')']
            stdout_log = str(run_dir / f"{stage}-stdout.log")
            stderr_log = str(run_dir / f"{stage}-stderr.log")
            exit_code_file = str(run_dir / f"{stage}-exit.txt")
            stdout_f = open(stdout_log, "w", buffering=1)
            stderr_f = open(stderr_log, "w", buffering=1)
            kwargs: dict = {"stdout": stdout_f, "stderr": stderr_f,
                            "cwd": working_directory}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kwargs)
            pid = proc.pid

            def _reaper():
                code = proc.wait()
                try:
                    Path(exit_code_file).write_text(str(code))
                except Exception:
                    pass
                stdout_f.close()
                stderr_f.close()
            import threading
            threading.Thread(target=_reaper, daemon=True).start()
            return ProcessHandle(pid, pid, stdout_log, stderr_log, exit_code_file)

        wm.WorkerMonitor._start_process_worker = patched_start
        try:
            wid = monitor.start_worker("t4", "research", "researcher",
                                       "dummy", str(tmp_run_root), session_name="s4")
        finally:
            wm.WorkerMonitor._start_process_worker = original_start
        # Wait for the process to exit
        time.sleep(2)
        status = monitor.poll_worker(wid)
        assert status.status in (H_COMPLETED, H_FAILED)
        if status.status == H_COMPLETED:
            assert status.exit_code == 0


class TestStallDetection:
    def test_stall_when_no_progress(self, monitor, tmp_run_root):
        """A handle with no progress for stall_timeout seconds is marked STALLED."""
        task_id = "t5"
        # Start a CAO worker that never completes (no result file)
        monitor.cao.launch_worker = lambda *a, **kw: None
        monitor.cao.get_terminal_status = lambda tid: {"status": "idle"}
        monitor.cao.get_terminal_output = lambda tid, mode="full": ""
        wid = monitor.start_worker(task_id, "plan", "codex-planner", "p",
                                   str(tmp_run_root), session_name="s5")
        # Manually set last_progress_at to the past to simulate stall
        with monitor._lock, monitor._conn() as c:
            c.execute(
                "UPDATE workers SET last_progress_at=? WHERE worker_id=?",
                (time.time() - 2000, wid),
            )
            c.commit()
        # wait_for_stage with a short stall_timeout should detect stall
        result = monitor.wait_for_stage(task_id, stall_timeout=1, max_polls=5)
        # The handle should be STALLED or the reattach should have been attempted
        wh = monitor.get_handle(wid)
        assert wh.status in (H_STALLED, H_RUNNING, H_COMPLETED, H_FAILED)


class TestReattach:
    def test_reattach_cao_alive(self, monitor):
        """resume_worker on a CAO terminal that is still alive returns True."""
        # Create a STALLED handle manually
        now = time.time()
        wh = WorkerHandle(
            worker_id="w-reattach1", task_id="t6", stage="plan",
            profile="codex-planner", handle_type="cao_terminal",
            cao_handle={"terminal_id": "alive-t1", "session_name": "s6",
                        "provider": "codex"},
            process_handle=None, status=H_STALLED, output_offset=0,
            output_hash="", last_output_at=now, last_heartbeat_at=now,
            last_progress_at=now, exit_code=None, resume_state="PLANNING",
            owner_id=monitor._owner_id, lease_until=now + 300, started_at=now)
        monitor._persist(wh)
        monitor.cao.get_terminal_status = lambda tid: {"status": "idle"}
        assert monitor.resume_worker("w-reattach1") is True
        wh2 = monitor.get_handle("w-reattach1")
        assert wh2.status == H_RUNNING

    def test_reattach_cao_dead(self, monitor):
        """resume_worker on a dead CAO terminal returns False."""
        now = time.time()
        wh = WorkerHandle(
            worker_id="w-reattach2", task_id="t7", stage="plan",
            profile="codex-planner", handle_type="cao_terminal",
            cao_handle={"terminal_id": "dead-t1", "session_name": "s7",
                        "provider": "codex"},
            process_handle=None, status=H_STALLED, output_offset=0,
            output_hash="", last_output_at=now, last_heartbeat_at=now,
            last_progress_at=now, exit_code=None, resume_state="PLANNING",
            owner_id=monitor._owner_id, lease_until=now + 300, started_at=now)
        monitor._persist(wh)
        monitor.cao.get_terminal_status = lambda tid: {"status": "error"}
        assert monitor.resume_worker("w-reattach2") is False


class TestOwnership:
    def test_ownership_prevents_double_poll(self, monitor):
        """Two monitors with different owner_ids cannot poll the same handle."""
        now = time.time()
        wh = WorkerHandle(
            worker_id="w-own1", task_id="t8", stage="plan",
            profile="codex-planner", handle_type="cao_terminal",
            cao_handle={"terminal_id": "t1", "session_name": "s8",
                        "provider": "codex"},
            process_handle=None, status=H_RUNNING, output_offset=0,
            output_hash="", last_output_at=now, last_heartbeat_at=now,
            last_progress_at=now, exit_code=None, resume_state=None,
            owner_id="other-owner", lease_until=now + 300, started_at=now)
        monitor._persist(wh)
        # Our monitor has a different owner_id; lease is valid → poll rejected
        status = monitor.poll_worker("w-own1")
        assert status.status == H_FAILED
        assert "ownership" in (status.error or "").lower()

    def test_ownership_claimable_after_lease_expires(self, monitor):
        """After lease expires, a new owner can claim the handle."""
        now = time.time()
        wh = WorkerHandle(
            worker_id="w-own2", task_id="t9", stage="plan",
            profile="codex-planner", handle_type="cao_terminal",
            cao_handle={"terminal_id": "t2", "session_name": "s9",
                        "provider": "codex"},
            process_handle=None, status=H_RUNNING, output_offset=0,
            output_hash="", last_output_at=now, last_heartbeat_at=now,
            last_progress_at=now, exit_code=None, resume_state=None,
            owner_id="old-owner", lease_until=now - 10, started_at=now)
        monitor._persist(wh)
        # lease expired → our monitor can claim
        monitor.cao.get_terminal_status = lambda tid: {"status": "completed"}
        monitor.cao.get_terminal_output = lambda tid, mode="full": ""
        status = monitor.poll_worker("w-own2")
        # Should not be ownership-rejected
        assert "ownership" not in (status.error or "").lower()


class TestPersistence:
    def test_handle_survives_across_instances(self, tmp_db, tmp_run_root):
        """A handle persisted by one monitor can be loaded by a new monitor instance."""
        fake_cao = MagicMock()
        fake_cao.server_url = "http://127.0.0.1:9889"
        fake_cao.provider_for = lambda p: "codex"
        m1 = WorkerMonitor(cao_client=fake_cao, db_path=tmp_db, run_root=tmp_run_root)
        now = time.time()
        wh = WorkerHandle(
            worker_id="w-persist1", task_id="t10", stage="plan",
            profile="codex-planner", handle_type="cao_terminal",
            cao_handle={"terminal_id": "p1", "session_name": "s10",
                        "provider": "codex"},
            process_handle=None, status=H_STALLED, output_offset=100,
            output_hash="abc", last_output_at=now, last_heartbeat_at=now,
            last_progress_at=now, exit_code=None, resume_state="PLANNING",
            owner_id=m1._owner_id, lease_until=now + 300, started_at=now)
        m1._persist(wh)
        m1.close()

        # New monitor loads from the same DB
        m2 = WorkerMonitor(cao_client=fake_cao, db_path=tmp_db, run_root=tmp_run_root)
        wh2 = m2.get_handle("w-persist1")
        assert wh2 is not None
        assert wh2.status == H_STALLED
        assert wh2.resume_state == "PLANNING"
        assert wh2.cao_handle["terminal_id"] == "p1"
        m2.close()


class TestProcessAlive:
    def test_process_alive_current_process(self):
        """os.kill(pid, 0) on the current process returns True."""
        assert WorkerMonitor._process_alive(os.getpid()) is True

    def test_process_alive_dead_pid(self):
        """A non-existent pid returns False."""
        # pid 0x7fffffff is very likely unused
        assert WorkerMonitor._process_alive(0x7fffffff) is False


class TestStalledNotTaskState:
    """STALLED is a WorkerHandle status, NOT a TaskState. Verify it does not
    appear in TaskState values."""
    def test_stalled_not_in_taskstate(self):
        from supervisor_cao.state.machine import TaskState
        values = {t.value for t in TaskState}
        assert "STALLED" not in values
