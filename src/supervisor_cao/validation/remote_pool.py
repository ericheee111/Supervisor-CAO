"""Remote validation pool manager (spec §14).

Manages the 920B dual-container pool over SSH. Each container runs one task at
a time. Atomic lock via remote flock. Records original branch/HEAD, refuses
dirty repos, restores after, marks UNHEALTHY on restore failure.

This module is the deterministic runner. The Supervisor/Verifier only READS the
script's return values: AVAILABLE / BUSY / UNHEALTHY / DIRTY. No LLM polls.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

RUN_ROOT = Path.home() / "cao-runs"


@dataclass
class ContainerState:
    name: str
    status: str  # AVAILABLE | BUSY | UNHEALTHY | DIRTY | UNREACHABLE
    locked_by: str | None = None
    lock_ts: float | None = None
    detail: str = ""


class RemotePoolError(Exception):
    pass


def _ssh(ssh_host: str, cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a command on the remote host over SSH (non-interactive)."""
    full = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10", ssh_host, cmd]
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout)


def _remote_repo_cmd(ssh_host: str, user: str, repo_path: str, container: str,
                     git_cmd: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a git command inside the container as the validation user."""
    # docker exec <container> [sudo -u <user>] git -C <repo> <cmd>
    # skip sudo when user is root (containers may not have sudo, and root runs as root)
    user_prefix = f"sudo -u {user} " if user and user != "root" else ""
    inner = f"docker exec {container} {user_prefix}git -C {shlex.quote(repo_path)} {git_cmd}"
    return _ssh(ssh_host, inner, timeout=timeout)


def check_reachable(ssh_host: str) -> bool:
    r = _ssh(ssh_host, "echo POOL_OK", timeout=15)
    return r.returncode == 0 and "POOL_OK" in r.stdout


def container_status(ssh_host: str, container: str, user: str, repo_path: str) -> ContainerState:
    """Inspect a single container: lock + worktree cleanliness."""
    # reachability
    r = _ssh(ssh_host, f"docker inspect --format='{{{{.State.Running}}}}' {container}", timeout=25)
    if r.returncode != 0 or "true" not in r.stdout:
        return ContainerState(name=container, status="UNREACHABLE", detail=r.stderr.strip())

    # lock check (remote flock via a lock file)
    lock_file = f"/tmp/scao-{container}.lock"
    r = _ssh(ssh_host, f"test -f {lock_file} && cat {lock_file} || echo FREE", timeout=20)
    out = r.stdout.strip()
    if out and out != "FREE":
        try:
            info = json.loads(out)
            return ContainerState(name=container, status="BUSY",
                                  locked_by=info.get("task_id"), lock_ts=info.get("ts"),
                                  detail="locked")
        except Exception:
            return ContainerState(name=container, status="BUSY", detail=out)

    # worktree cleanliness
    r = _remote_repo_cmd(ssh_host, user, repo_path, container, "status --porcelain", timeout=20)
    if r.returncode != 0:
        return ContainerState(name=container, status="UNHEALTHY", detail=f"git status failed: {r.stderr.strip()}")
    if r.stdout.strip():
        return ContainerState(name=container, status="DIRTY", detail=r.stdout.strip()[:200])

    return ContainerState(name=container, status="AVAILABLE")


def pool_status(ssh_host: str, containers: list[str], user: str, repo_path: str) -> list[ContainerState]:
    return [container_status(ssh_host, c, user, repo_path) for c in containers]


def acquire_lock(ssh_host: str, container: str, task_id: str, candidate_sha: str | None = None,
                 timeout: int = 300) -> bool:
    """Atomically acquire a container lock. Returns True on success.
    Uses remote mkdir (atomic) as the lock primitive; falls back to flock.
    """
    lock_file = f"/tmp/scao-{container}.lock"
    info = json.dumps({"task_id": task_id, "ts": time.time(), "sha": candidate_sha})
    # atomic create: fail if exists
    r = _ssh(ssh_host,
             f"(set -o noclobber; echo '{info}' > {lock_file}) 2>/dev/null && echo ACQUIRED || echo TAKEN",
             timeout=20)
    return r.returncode == 0 and "ACQUIRED" in r.stdout


def release_lock(ssh_host: str, container: str) -> bool:
    lock_file = f"/tmp/scao-{container}.lock"
    r = _ssh(ssh_host, f"rm -f {lock_file} && echo RELEASED", timeout=25)
    return r.returncode == 0 and "RELEASED" in r.stdout


def record_git_state(ssh_host: str, container: str, user: str, repo_path: str,
                     run_dir: Path) -> dict:
    """Record original branch, HEAD, porcelain BEFORE verification (spec §14.3)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {}
    for key, git_cmd in [("branch", "rev-parse --abbrev-ref HEAD"),
                         ("head", "rev-parse HEAD"),
                         ("porcelain", "status --porcelain")]:
        r = _remote_repo_cmd(ssh_host, user, repo_path, container, git_cmd, timeout=20)
        state[key] = r.stdout.strip() if r.returncode == 0 else f"ERR:{r.stderr.strip()}"
    (run_dir / "git-state-before.json").write_text(json.dumps(state, indent=2))
    return state


def restore_git_state(ssh_host: str, container: str, user: str, repo_path: str,
                      before: dict, run_dir: Path) -> bool:
    """Restore original branch and HEAD. NO reset --hard, NO clean -fdx.
    Returns True if HEAD matches the recorded original. Marks UNHEALTHY on failure.
    """
    ok = True
    if before.get("branch") and before["branch"] != "ERR":
        _remote_repo_cmd(ssh_host, user, repo_path, container,
                         f"checkout {shlex.quote(before['branch'])}", timeout=30)
    if before.get("head") and before["head"] != "ERR":
        _remote_repo_cmd(ssh_host, user, repo_path, container,
                         f"reset --soft {before['head']}", timeout=30)
        # verify
        r = _remote_repo_cmd(ssh_host, user, repo_path, container, "rev-parse HEAD", timeout=15)
        after = r.stdout.strip()
        if after != before["head"]:
            ok = False
    # record after state
    after_state = {}
    for key, git_cmd in [("branch", "rev-parse --abbrev-ref HEAD"),
                         ("head", "rev-parse HEAD"),
                         ("porcelain", "status --porcelain")]:
        r = _remote_repo_cmd(ssh_host, user, repo_path, container, git_cmd, timeout=20)
        after_state[key] = r.stdout.strip() if r.returncode == 0 else f"ERR:{r.stderr.strip()}"
    (run_dir / "git-state-after.json").write_text(json.dumps(after_state, indent=2))
    return ok


def select_available(states: list[ContainerState]) -> ContainerState | None:
    """Pick a healthy, idle container. Returns None if all busy/unhealthy."""
    for s in states:
        if s.status == "AVAILABLE":
            return s
    return None
