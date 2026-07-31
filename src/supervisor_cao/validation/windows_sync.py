"""Windows repository synchronization (spec §17).

Only the platform sync script may touch the Windows repo. Sync happens AFTER
PR content generation. Fast-forward only; never reset --hard, never
overwrite dirty trees, never force checkout, never cherry-pick, never merge dev.

Gates (ALL must hold):
  - candidate pushed
  - tested_sha == candidate_sha
  - reviewed_sha == candidate_sha
  - Review APPROVED
  - pr-content artifact valid (sha256, schema, workflow_state, SHAs, push.json)
  - Windows worktree clean
  - local task branch fast-forwardable

Final check: Windows HEAD == candidate SHA.
"""
from __future__ import annotations

import hashlib
import json
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
    pr_content_ready: bool
    windows_clean: bool
    fast_forwardable: bool

    @property
    def all_pass(self) -> bool:
        return all([
            self.candidate_pushed, self.tested_eq_candidate, self.reviewed_eq_candidate,
            self.review_approved, self.pr_content_ready, self.windows_clean, self.fast_forwardable,
        ])


def validate_pr_content_artifact(run_dir: str | Path, task_id: str,
                                 candidate_sha: str, tested_sha: str,
                                 reviewed_sha: str, base_branch: str,
                                 head_branch: str) -> bool:
    """Validate the pr-content artifact package. Returns True only if ALL checks pass.

    Checks:
      - pr-content.sha256 exists and has correct two-line format
      - JSON hash matches
      - Markdown hash matches
      - schema_version == 1
      - task_id matches
      - workflow_state == "PR_CONTENT_READY"
      - base_branch matches
      - head_branch matches
      - candidate/tested/reviewed SHA match
      - review_decision == "APPROVED"
      - push.json: push_succeeded, pushed_sha == candidate, branch == head_branch
    """
    rd = Path(run_dir)
    sha_path = rd / "pr-content.sha256"
    json_path = rd / "pr-content.json"
    md_path = rd / "pr-content.md"
    push_path = rd / "push.json"
    if not (sha_path.exists() and json_path.exists() and md_path.exists()):
        return False
    try:
        sha_text = sha_path.read_text(encoding="utf-8")
        json_text = json_path.read_text(encoding="utf-8")
        md_text = md_path.read_text(encoding="utf-8")
        j = json.loads(json_text)
    except Exception:
        return False
    # checksum: two-line format
    lines = sha_text.strip().split("\n")
    if len(lines) != 2:
        return False
    expected_j = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    expected_m = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    if lines[0].split()[0] != expected_j or "pr-content.json" not in lines[0]:
        return False
    if lines[1].split()[0] != expected_m or "pr-content.md" not in lines[1]:
        return False
    # field validation
    if j.get("schema_version") != 1:
        return False
    if j.get("task_id") != task_id:
        return False
    if j.get("workflow_state") != "PR_CONTENT_READY":
        return False
    if j.get("base_branch") != base_branch:
        return False
    if j.get("head_branch") != head_branch:
        return False
    if j.get("candidate_sha") != candidate_sha:
        return False
    if j.get("tested_sha") != tested_sha:
        return False
    if j.get("reviewed_sha") != reviewed_sha:
        return False
    if j.get("review_decision") != "APPROVED":
        return False
    # push.json
    if not push_path.exists():
        return False
    try:
        push = json.loads(push_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not push.get("push_succeeded"):
        return False
    if push.get("pushed_sha") != candidate_sha:
        return False
    if push.get("branch") != head_branch:
        return False
    return True


def check_gates(windows_repo: str, task_branch: str, candidate_sha: str,
                tested_sha: str | None, reviewed_sha: str | None,
                review_approved: bool, *,
                run_dir: str | Path | None = None,
                task_id: str = "", base_branch: str = "",
                head_branch: str = "") -> SyncGates:
    """Evaluate all sync gates without modifying anything."""
    win_clean = _git_porcelain_clean(windows_repo)
    _run(["git", "-C", windows_repo, "fetch", "origin"], check=False)
    r = _run(["git", "-C", windows_repo, "rev-parse", f"origin/{task_branch}"], check=False)
    candidate_pushed = r.returncode == 0 and r.stdout.strip() == candidate_sha
    r = _run(["git", "-C", windows_repo, "rev-parse", task_branch], check=False)
    if r.returncode == 0:
        local_sha = r.stdout.strip()
        r2 = _run(["git", "-C", windows_repo, "merge-base", "--is-ancestor",
                   local_sha, f"origin/{task_branch}"], check=False)
        ff = r2.returncode == 0
    else:
        ff = True  # branch doesn't exist locally -> will create tracking branch
    pr_ready = False
    if run_dir and task_id:
        pr_ready = validate_pr_content_artifact(
            run_dir, task_id, candidate_sha,
            tested_sha or "", reviewed_sha or "", base_branch, head_branch)
    return SyncGates(
        candidate_pushed=candidate_pushed,
        tested_eq_candidate=(tested_sha == candidate_sha) if tested_sha else False,
        reviewed_eq_candidate=(reviewed_sha == candidate_sha) if reviewed_sha else False,
        review_approved=review_approved,
        pr_content_ready=pr_ready,
        windows_clean=win_clean,
        fast_forwardable=ff,
    )


def sync(windows_repo: str, task_branch: str, candidate_sha: str,
         tested_sha: str | None, reviewed_sha: str | None,
         review_approved: bool, *,
         run_dir: str | Path | None = None,
         task_id: str = "", base_branch: str = "",
         head_branch: str = "") -> str:
    """Perform the protected Windows sync. Returns the Windows HEAD SHA after sync.

    Raises WindowsSyncBlocked if any gate fails (status WINDOWS_SYNC_BLOCKED).
    """
    gates = check_gates(windows_repo, task_branch, candidate_sha,
                        tested_sha, reviewed_sha, review_approved,
                        run_dir=run_dir, task_id=task_id,
                        base_branch=base_branch, head_branch=head_branch)
    if not gates.all_pass:
        failed = [k for k, v in gates.__dict__.items() if not v]
        raise WindowsSyncBlocked(f"WINDOWS_SYNC_BLOCKED: failed gates {failed}")

    _run(["git", "-C", windows_repo, "fetch", "origin"])
    r = _run(["git", "-C", windows_repo, "rev-parse", task_branch], check=False)
    if r.returncode != 0:
        _run(["git", "-C", windows_repo, "checkout", "-B", task_branch, f"origin/{task_branch}"])
    else:
        _run(["git", "-C", windows_repo, "checkout", task_branch])
    _run(["git", "-C", windows_repo, "merge", "--ff-only", f"origin/{task_branch}"])
    r = _run(["git", "-C", windows_repo, "rev-parse", "HEAD"])
    final_sha = r.stdout.strip()
    if final_sha != candidate_sha:
        raise WindowsSyncBlocked(
            f"WINDOWS_SYNC_BLOCKED: Windows HEAD {final_sha} != candidate {candidate_sha}"
        )
    return final_sha
