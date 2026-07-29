"""Git worktree management (spec §12).

Per-task isolated worktrees under ~/cao-worktrees/pandas/<task-id>/{executor,verifier,reviewer}.
- executor: writable, on agent/<task-id> branch
- verifier/reviewer: read-only checkouts of the candidate SHA
- main clone (~/projects/pandas) only used for fetch/branch/worktree mgmt, never edited.
- no force push, no base-branch rewrite, every valid candidate committed+pushed.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKTREE_ROOT = Path.home() / "cao-worktrees"


class WorktreeError(Exception):
    pass


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise WorktreeError(f"cmd {cmd[0]} failed ({r.returncode}): {r.stderr.strip()}")
    return r


@dataclass
class WorktreePaths:
    task_id: str
    root: Path
    executor: Path
    verifier: Path
    reviewer: Path


def task_worktree_root(project: str, task_id: str) -> Path:
    return WORKTREE_ROOT / project / task_id


def paths_for(project: str, task_id: str) -> WorktreePaths:
    root = task_worktree_root(project, task_id)
    return WorktreePaths(
        task_id=task_id, root=root,
        executor=root / "executor", verifier=root / "verifier", reviewer=root / "reviewer",
    )


def git_porcelain_clean(repo: str) -> bool:
    """Return True if `git status --porcelain` is empty (clean worktree)."""
    r = _run(["git", "-C", repo, "status", "--porcelain"])
    return r.stdout.strip() == ""


def fetch_main(main_repo: str, base_branch: str = "dev") -> str:
    """Fetch origin and return the latest base-branch SHA. Does NOT modify working tree."""
    _run(["git", "-C", main_repo, "fetch", "origin"])
    r = _run(["git", "-C", main_repo, "rev-parse", f"origin/{base_branch}"])
    return r.stdout.strip()


def create_task_branch(main_repo: str, task_id: str, base_branch: str = "dev") -> str:
    """Create agent/<task-id> from latest origin/<base> in the main clone (no checkout).
    Returns the new branch HEAD SHA. Idempotent: if branch exists, returns its SHA.
    """
    branch = f"agent/{task_id}"
    # ensure branch doesn't already diverge
    r = _run(["git", "-C", main_repo, "rev-parse", "--verify", branch], check=False)
    if r.returncode == 0:
        return r.stdout.strip()
    base_sha = fetch_main(main_repo, base_branch)
    _run(["git", "-C", main_repo, "branch", branch, base_sha])
    return base_sha


def add_executor_worktree(main_repo: str, project: str, task_id: str) -> Path:
    """Create the writable executor worktree on agent/<task-id>. Idempotent."""
    p = paths_for(project, task_id)
    if p.executor.exists() and (p.executor / ".git").exists():
        return p.executor
    p.executor.mkdir(parents=True, exist_ok=True)
    branch = f"agent/{task_id}"
    _run(["git", "-C", main_repo, "worktree", "add", str(p.executor), branch], check=False)
    # if worktree add failed because branch not local, create tracking
    if not (p.executor / ".git").exists():
        _run(["git", "-C", main_repo, "worktree", "add", "-B", branch, str(p.executor), f"origin/{branch}"],
             check=False)
    if not (p.executor / ".git").exists():
        raise WorktreeError(f"failed to create executor worktree at {p.executor}")
    return p.executor


def add_readonly_worktree(main_repo: str, project: str, task_id: str, role: str,
                          sha: str | None = None) -> Path:
    """Create a read-only worktree (verifier/reviewer) at an optional detached SHA.
    Read-only-ness is enforced by the policy layer (permissions), not git itself.
    """
    if role not in ("verifier", "reviewer"):
        raise WorktreeError(f"unknown read-only role {role}")
    p = paths_for(project, task_id)
    target = getattr(p, role)
    if target.exists() and (target / ".git").exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    branch = f"agent/{task_id}-{role}"
    if sha:
        _run(["git", "-C", main_repo, "worktree", "add", "--detach", str(target), sha], check=False)
    else:
        _run(["git", "-C", main_repo, "worktree", "add", "-B", branch, str(target), f"agent/{task_id}"],
             check=False)
    if not (target / ".git").exists():
        raise WorktreeError(f"failed to create {role} worktree at {target}")
    return target


def current_sha(repo: str) -> str:
    r = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    return r.stdout.strip()


def commit_and_push(repo: str, branch: str, message: str, *, remote: str = "origin") -> str:
    """Stage all, commit, push the task branch. Refuses on dirty-after or push failure.
    Returns the new HEAD SHA. Does NOT force push.
    """
    _run(["git", "-C", repo, "add", "-A"])
    r = _run(["git", "-C", repo, "diff", "--cached", "--quiet"], check=False)
    if r.returncode == 0:
        raise WorktreeError("nothing to commit (empty diff) - no progress")
    _run(["git", "-C", repo, "commit", "-m", message])
    # push WITHOUT --force
    _run(["git", "-C", repo, "push", remote, branch])
    return current_sha(repo)


def remove_worktrees(project: str, task_id: str, main_repo: str) -> None:
    """Remove all worktrees for a task (cleanup). Refuses if executor is dirty."""
    p = paths_for(project, task_id)
    for wt in (p.executor, p.verifier, p.reviewer):
        if wt.exists() and (wt / ".git").exists():
            if wt == p.executor and not git_porcelain_clean(str(wt)):
                raise WorktreeError(f"executor worktree dirty, refusing removal: {wt}")
            _run(["git", "-C", main_repo, "worktree", "remove", str(wt)], check=False)
