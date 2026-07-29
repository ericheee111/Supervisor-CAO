"""Windows repository synchronization (spec §17).

Only the platform sync script may touch the Windows repo. Sync happens AFTER
READY_FOR_HUMAN_REVIEW gate. Fast-forward only; never reset --hard, never
overwrite dirty trees, never force checkout, never cherry-pick, never merge dev.

Gates (ALL must hold):
  - candidate pushed
  - tested_sha == candidate_sha
  - reviewed_sha == candidate_sha
  - Review APPROVED
  - Draft PR created/updated
  - Windows worktree clean
  - local task branch fast-forwardable

Final check: Windows HEAD == candidate SHA.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class WindowsSyncBlocked(Exception):
    pass


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise WindowsSyncBlocked(f"cmd failed ({r.returncode}): {' '.join(cmd[:4])}...: {r.stderr.strip()}")
    return r


def _git_porcelain_clean(repo: str) -> bool:
    r = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0 and r.stdout.strip() == ""


@dataclass
class SyncGates:
    candidate_pushed: bool
    tested_eq_candidate: bool
    reviewed_eq_candidate: bool
    review_approved: bool
    draft_pr_created: bool
    windows_clean: bool
    fast_forwardable: bool

    @property
    def all_pass(self) -> bool:
        return all([
            self.candidate_pushed, self.tested_eq_candidate, self.reviewed_eq_candidate,
            self.review_approved, self.draft_pr_created, self.windows_clean, self.fast_forwardable,
        ])


def check_gates(windows_repo: str, task_branch: str, candidate_sha: str,
                tested_sha: str | None, reviewed_sha: str | None,
                review_approved: bool, draft_pr_created: bool) -> SyncGates:
    """Evaluate all sync gates without modifying anything."""
    # windows clean
    win_clean = _git_porcelain_clean(windows_repo)
    # fetch to know remote state (read-only fetch is safe)
    _run(["git", "-C", windows_repo, "fetch", "origin"], check=False)
    # candidate pushed: remote branch exists at candidate_sha
    r = _run(["git", "-C", windows_repo, "rev-parse", f"origin/{task_branch}"], check=False)
    candidate_pushed = r.returncode == 0 and r.stdout.strip() == candidate_sha
    # ff-able: local branch (if exists) is ancestor of origin/task_branch
    r = _run(["git", "-C", windows_repo, "rev-parse", task_branch], check=False)
    if r.returncode == 0:
        local_sha = r.stdout.strip()
        r2 = _run(["git", "-C", windows_repo, "merge-base", "--is-ancestor", local_sha, f"origin/{task_branch}"], check=False)
        ff = r2.returncode == 0
    else:
        ff = True  # branch doesn't exist locally -> will create tracking branch
    return SyncGates(
        candidate_pushed=candidate_pushed,
        tested_eq_candidate=(tested_sha == candidate_sha) if tested_sha else False,
        reviewed_eq_candidate=(reviewed_sha == candidate_sha) if reviewed_sha else False,
        review_approved=review_approved,
        draft_pr_created=draft_pr_created,
        windows_clean=win_clean,
        fast_forwardable=ff,
    )


def sync(windows_repo: str, task_branch: str, candidate_sha: str,
         tested_sha: str | None, reviewed_sha: str | None,
         review_approved: bool, draft_pr_created: bool) -> str:
    """Perform the protected Windows sync. Returns the Windows HEAD SHA after sync.
    Raises WindowsSyncBlocked if any gate fails (status WINDOWS_SYNC_BLOCKED).
    """
    gates = check_gates(windows_repo, task_branch, candidate_sha,
                        tested_sha, reviewed_sha, review_approved, draft_pr_created)
    if not gates.all_pass:
        failed = [k for k, v in gates.__dict__.items() if not v]
        raise WindowsSyncBlocked(f"WINDOWS_SYNC_BLOCKED: failed gates {failed}")

    # fetch (already done in check_gates, redo to be safe)
    _run(["git", "-C", windows_repo, "fetch", "origin"])
    # checkout task branch (create tracking if missing) - NO force
    r = _run(["git", "-C", windows_repo, "rev-parse", task_branch], check=False)
    if r.returncode != 0:
        _run(["git", "-C", windows_repo, "checkout", "-B", task_branch, f"origin/{task_branch}"])
    else:
        _run(["git", "-C", windows_repo, "checkout", task_branch])
    # fast-forward only merge
    _run(["git", "-C", windows_repo, "merge", "--ff-only", f"origin/{task_branch}"])
    # final verification
    r = _run(["git", "-C", windows_repo, "rev-parse", "HEAD"])
    final_sha = r.stdout.strip()
    if final_sha != candidate_sha:
        raise WindowsSyncBlocked(
            f"WINDOWS_SYNC_BLOCKED: Windows HEAD {final_sha} != candidate {candidate_sha}"
        )
    return final_sha
