"""Task runner CLI: task start/watch/resume/status/logs.

Provides the user-facing interface for running Supervisor-CAO tasks.
task start drives a task to terminal (APPROVED/FAILED/NEEDS_HUMAN).
Ctrl+C releases the Controller lease without killing the Worker.
task resume reads the config snapshot and Worker handle from SQLite.
task watch/logs are read-only (peek_worker, no lease).
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from supervisor_cao.state.machine import StateStore, TaskState
from supervisor_cao.budget.codex import CodexBudget
from supervisor_cao.mcp.stage_store import StageStore
from supervisor_cao.mcp.policy_gateway import PolicyGateway
from supervisor_cao.mcp.worker_monitor import WorkerMonitor
from supervisor_cao.mcp.cao_client import CaoClient
from supervisor_cao.projects.config import ProjectConfig, load_project

RUN_ROOT = Path.home() / "cao-runs"

# Runtime terminal states (APPROVED is the success terminal for this round)
RUNTIME_TERMINAL = {
    TaskState.APPROVED.value,
    TaskState.FAILED.value,
    TaskState.NEEDS_HUMAN.value,
}


def _build_gateway(run_root: Path | None = None) -> PolicyGateway:
    """Build a production PolicyGateway (real CaoClient, no local_fixture)."""
    run_root = run_root or RUN_ROOT
    store = StateStore()
    budget = CodexBudget()
    stages = StageStore()
    cao = CaoClient(run_root=run_root)
    return PolicyGateway(
        state_store=store, budget=budget, cao_client=cao,
        stage_store=stages, run_root=run_root, test_mode=False)


def _make_temp_config(repo: str, base_branch: str, verify_command: str | None,
                      stall_timeout: int = 1800) -> ProjectConfig:
    """Build a temporary ProjectConfig for --repo mode."""
    return ProjectConfig(
        name="temp-task",
        base_branch=base_branch,
        wsl_repo=repo,
        remote_verification_mode="disabled",
        default_verification={"local": {"command": verify_command.split()}} if verify_command else {},
        stall_timeout=stall_timeout,
    )


def task_start(repo: str, base_branch: str, description_file: str,
               project: str | None = None, verify_command: str | None = None,
               stall_timeout: int = 1800) -> int:
    """Start a new task and drive it to terminal."""
    desc = Path(description_file).read_text(encoding="utf-8")
    task_id = f"task-{int(time.time())}"
    run_root = RUN_ROOT
    gw = _build_gateway(run_root)

    # Resolve config
    if project:
        cfg = load_project(project)
    else:
        if not verify_command:
            print("ERROR: --verify-command required in temp repo mode (no --project)",
                  file=sys.stderr)
            return 1
        cfg = _make_temp_config(repo, base_branch, verify_command, stall_timeout)

    # Persist config snapshot
    gw.save_config_snapshot(task_id, cfg)

    # Create task
    print(f"Task ID: {task_id}")
    print(f"Project: {cfg.name}")
    print(f"Base: {base_branch}")
    gw.create_task(task_id, cfg.name, desc, baseline_sha=None)

    # Monkeypatch load_project to return our cfg
    import supervisor_cao.mcp.policy_gateway as pg_mod
    original = pg_mod.load_project
    pg_mod.load_project = lambda name: cfg

    # Set up Ctrl+C handler (release lease, don't kill Worker)
    def _ctrl_c(signum, frame):
        print("\n\n=== Ctrl+C: releasing lease (Worker continues running) ===")
        wh = gw.worker_monitor.find_for_task(task_id)
        if wh:
            gw.worker_monitor.release_ownership(wh.worker_id)
        print(f"Resume with: supervisor-cao task resume {task_id}")
        sys.exit(130)
    signal.signal(signal.SIGINT, _ctrl_c)

    try:
        _drive_to_terminal(gw, task_id, stall_timeout)
    finally:
        pg_mod.load_project = original

    rec = gw.get_task(task_id)
    print(f"\nFinal state: {rec['state']}")
    return 0 if rec["state"] == TaskState.APPROVED.value else 1


def task_resume(task_id: str, stall_timeout: int = 1800) -> int:
    """Resume an interrupted task."""
    run_root = RUN_ROOT
    run_dir = run_root / task_id
    if not run_dir.exists():
        print(f"ERROR: task not found: {task_id}", file=sys.stderr)
        return 1

    # Load config snapshot (do NOT re-load mutable config)
    cfg = PolicyGateway.load_config_snapshot(run_dir)
    if cfg is None:
        print(f"ERROR: no config snapshot for task {task_id}", file=sys.stderr)
        return 1

    gw = _build_gateway(run_root)

    # Safe takeover of orphaned Worker handle
    wh = gw.worker_monitor.find_for_task(task_id)
    if wh:
        if gw.worker_monitor.safe_takeover(wh.worker_id):
            print(f"Safe takeover of worker {wh.worker_id}")
        else:
            print(f"WARNING: cannot take over worker (owner still alive or lease valid)")

    # Monkeypatch load_project
    import supervisor_cao.mcp.policy_gateway as pg_mod
    original = pg_mod.load_project
    pg_mod.load_project = lambda name: cfg

    def _ctrl_c(signum, frame):
        print("\n\n=== Ctrl+C: releasing lease (Worker continues running) ===")
        wh2 = gw.worker_monitor.find_for_task(task_id)
        if wh2:
            gw.worker_monitor.release_ownership(wh2.worker_id)
        print(f"Resume with: supervisor-cao task resume {task_id}")
        sys.exit(130)
    signal.signal(signal.SIGINT, _ctrl_c)

    try:
        _drive_to_terminal(gw, task_id, stall_timeout)
    finally:
        pg_mod.load_project = original

    rec = gw.get_task(task_id)
    print(f"\nFinal state: {rec['state']}")
    return 0 if rec["state"] == TaskState.APPROVED.value else 1


def task_watch(task_id: str, json_output: bool = False, follow: bool = False,
               poll_interval: int = 5, stall_timeout: int = 1800) -> int:
    """Watch a task (read-only, no lease acquisition)."""
    gw = _build_gateway()
    run_dir = RUN_ROOT / task_id

    def _print_snapshot():
        rec = gw.get_task(task_id)
        if not rec:
            print(f"Task not found: {task_id}", file=sys.stderr)
            return False
        wh = gw.worker_monitor.find_for_task(task_id)
        budget = gw.budget.summary(task_id)
        stages = gw.stages.list_stages(task_id)

        if json_output:
            snapshot = {
                "task_id": task_id, "state": rec["state"],
                "candidate_sha": rec.get("candidate_sha"),
                "tested_sha": rec.get("tested_sha"),
                "reviewed_sha": rec.get("reviewed_sha"),
                "budget": budget,
                "stages": [s.to_dict() for s in stages],
            }
            if wh:
                snapshot["worker"] = {
                    "worker_id": wh.worker_id, "status": wh.status,
                    "handle_type": wh.handle_type,
                    "terminal_id": wh.cao_handle.get("terminal_id") if wh.cao_handle else None,
                    "pid": wh.process_handle.get("pid") if wh.process_handle else None,
                    "last_progress_at": wh.last_progress_at,
                }
            print(json.dumps(snapshot))
        else:
            print(f"\n--- Task {task_id} ---")
            print(f"State: {rec['state']}")
            if rec.get("candidate_sha"):
                print(f"Candidate: {rec['candidate_sha'][:12]}")
            if rec.get("tested_sha"):
                print(f"Tested:    {rec['tested_sha'][:12]}")
            if rec.get("reviewed_sha"):
                print(f"Reviewed:  {rec['reviewed_sha'][:12]}")
            print(f"Budget: {budget}")
            if wh:
                print(f"Worker: {wh.worker_id[:8]} status={wh.status} type={wh.handle_type}")
                if wh.cao_handle:
                    print(f"  terminal_id: {wh.cao_handle.get('terminal_id')}")
                if wh.process_handle:
                    print(f"  pid: {wh.process_handle.get('pid')}")
                elapsed = time.time() - wh.started_at
                print(f"  elapsed: {elapsed:.0f}s")
                since_progress = time.time() - wh.last_progress_at
                print(f"  last progress: {since_progress:.0f}s ago")
            # Show latest output (read-only peek)
            if wh:
                status = gw.worker_monitor.peek_worker(wh.worker_id)
                if status.raw_output:
                    # Show last 5 lines
                    lines = status.raw_output.strip().split("\n")[-5:]
                    print("  Output (last 5 lines):")
                    for line in lines:
                        print(f"    {line}")
            for s in stages:
                print(f"  Stage {s.stage}: {s.status} attempt={s.attempt}")
        return True

    while True:
        if not _print_snapshot():
            return 1
        rec = gw.get_task(task_id)
        if rec and rec["state"] in RUNTIME_TERMINAL:
            print(f"\nTask reached terminal state: {rec['state']}")
            return 0
        if not follow:
            return 0
        time.sleep(poll_interval)


def task_status(task_id: str) -> int:
    """Print a one-shot status snapshot."""
    gw = _build_gateway()
    rec = gw.get_task(task_id)
    if not rec:
        print(f"Task not found: {task_id}", file=sys.stderr)
        return 1
    print(f"Task: {task_id}")
    print(f"State: {rec['state']}")
    print(f"Candidate: {rec.get('candidate_sha') or '-'}")
    print(f"Tested:    {rec.get('tested_sha') or '-'}")
    print(f"Reviewed:  {rec.get('reviewed_sha') or '-'}")
    print(f"Error:     {rec.get('error') or '-'}")
    budget = gw.budget.summary(task_id)
    print(f"Budget: {budget}")
    stages = gw.stages.list_stages(task_id)
    for s in stages:
        print(f"  Stage {s.stage}: {s.status} attempt={s.attempt}")
    return 0


def _drive_to_terminal(gw: PolicyGateway, task_id: str,
                       stall_timeout: int = 1800, max_stages: int = 40) -> dict:
    """Drive run_next_stage until runtime terminal. Returns final task record."""
    for i in range(1, max_stages + 1):
        rec = gw.get_task(task_id)
        if rec["state"] in RUNTIME_TERMINAL:
            break
        print(f"  [stage {i}] {rec['state']} -> run_next_stage ...")
        rec = gw.run_next_stage(task_id)
        print(f"    state={rec['state']} cand={rec.get('candidate_sha') or '-'}"
              f" tested={rec.get('tested_sha') or '-'}"
              f" reviewed={rec.get('reviewed_sha') or '-'}"
              f" err={rec.get('error') or '-'}")
        if rec["state"] in RUNTIME_TERMINAL:
            break
    return gw.get_task(task_id)
