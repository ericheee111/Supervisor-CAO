#!/usr/bin/env python3
"""End-to-end test on a temporary repository (spec §20.3).

Creates a temp git repo, drives the full policy-layer flow:
  state machine CREATED -> ... -> READY_FOR_HUMAN_REVIEW
  + worktree create + commit + push (to local bare remote)
  + Codex budget (planner + full_review)
  + Windows sync gate check (blocked when dirty, passes when clean)
  + Draft PR body generation

This tests the DETERMINISTIC policy layer end-to-end. It does NOT call real
LLM/Codex agents (those are mocked as artifact files). Per spec §20.3:
"first-round failure injection is only for test fixtures, not production logic."
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.state.machine import StateStore, TaskState  # noqa: E402
from supervisor_cao.budget.codex import CodexBudget  # noqa: E402
from supervisor_cao.workers.worktrees import (  # noqa: E402
    create_task_branch, add_executor_worktree, commit_and_push, current_sha,
    git_porcelain_clean,
)
from supervisor_cao.validation.windows_sync import check_gates, sync as win_sync, WindowsSyncBlocked  # noqa: E402
from supervisor_cao.projects.config import ProjectConfig  # noqa: E402

from importlib.machinery import SourceFileLoader
pr_mod = SourceFileLoader(
    "create_draft_pr", str(Path(__file__).resolve().parents[2] / "scripts" / "create-draft-pr")
).load_module()


def git(cmd, cwd=None, check=True):
    r = subprocess.run(["git"] + cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {cmd[:2]} failed: {r.stderr.strip()}")
    return r


def setup_temp_repos(tmp: Path):
    """Create a bare remote + a main clone + a 'windows' clone."""
    bare = tmp / "remote.git"
    git(["init", "--bare", "-b", "main", str(bare)])
    main_repo = tmp / "main"
    git(["init", "-b", "main", str(main_repo)])
    git(["config", "user.email", "t@t.t"], cwd=str(main_repo))
    git(["config", "user.name", "tester"], cwd=str(main_repo))
    (main_repo / "README.md").write_text("# test project\n")
    git(["add", "-A"], cwd=str(main_repo))
    git(["commit", "-m", "init"], cwd=str(main_repo))
    git(["branch", "main"], cwd=str(main_repo), check=False)  # base branch already main
    git(["remote", "add", "origin", str(bare)], cwd=str(main_repo))
    git(["push", "origin", "main"], cwd=str(main_repo))
    # secondary clone (simulates a platform-side sync target)
    win_repo = tmp / "windows"
    git(["clone", "-b", "main", str(bare), str(win_repo)])
    git(["config", "user.email", "t@t.t"], cwd=str(win_repo))
    git(["config", "user.name", "tester"], cwd=str(win_repo))
    return str(main_repo), str(win_repo), str(bare)


def main() -> int:
    import time
    tmp = Path(tempfile.mkdtemp(prefix="scao_e2e_"))
    print(f"temp dir: {tmp}")
    results = []
    def check(name, ok, detail=""):
        mark = "✓" if ok else "✗"
        results.append((name, ok, detail))
        print(f"  {mark} {name}: {detail}")

    main_repo, win_repo, bare = setup_temp_repos(tmp)
    # unique task id per run to avoid worktree path collisions across stability runs
    task_id = f"e2e-{int(time.time()*1000) % 1000000}"
    cfg = ProjectConfig(name="testproj", base_branch="main", task_branch_prefix="agent/",
                        wsl_repo=main_repo, windows_repo=win_repo)

    # state store + budget in temp
    store = StateStore(db_path=tmp / "tasks.db")
    budget = CodexBudget(db_path=tmp / "codex.db")
    store.create(task_id, "testproj", baseline_sha="base")

    # 1. RESEARCH -> PLAN
    store.transition(task_id, TaskState.RESEARCHING)
    store.transition(task_id, TaskState.PLANNING)
    budget.spend(task_id, "planner", input_artifact="research.md")
    store.transition(task_id, TaskState.PLAN_READY)
    check("planner budget consumed", budget.used(task_id, "planner") == 1, "1/1")

    # 2. IMPLEMENT: create branch + worktree + commit + push
    store.transition(task_id, TaskState.IMPLEMENTING)
    sha1 = create_task_branch(main_repo, task_id, cfg.base_branch)
    wt = add_executor_worktree(main_repo, "testproj", task_id)
    (Path(wt) / "feature.py").write_text("def f(): return 42\n")
    candidate_sha = commit_and_push(wt, f"agent/{task_id}", "implement feature")
    store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=candidate_sha)
    check("executor commit+push", candidate_sha != sha1, f"sha={candidate_sha[:12]}")

    # 3. VERIFY (local + remote)
    store.transition(task_id, TaskState.LOCAL_VERIFYING)
    store.transition(task_id, TaskState.LOCAL_VERIFIED, tested_sha=candidate_sha)
    store.transition(task_id, TaskState.REMOTE_QUEUED)
    store.transition(task_id, TaskState.REMOTE_VERIFYING)
    store.transition(task_id, TaskState.REMOTE_VERIFIED)
    check("verified, tested==candidate", True, f"tested={store.get(task_id).tested_sha[:12]}")

    # 4. REVIEW (codex full_review)
    store.transition(task_id, TaskState.REVIEWING, reviewed_sha=candidate_sha)
    budget.spend(task_id, "full_review", input_artifact="verification.json", candidate_sha=candidate_sha)
    check("reviewer budget consumed", budget.used(task_id, "full_review") == 1, "total 2/4")
    store.transition(task_id, TaskState.APPROVED)

    # 5. Draft PR body generation
    body = pr_mod.build_pr_body(task_id, "implement feature", "base", candidate_sha,
                                ["feature.py"], {"pytest_passed": True}, {"asv": "ok"},
                                {"decision": "APPROVED"}, 2, ["artifacts/..."], [])
    check("draft PR body has READY_FOR_HUMAN_REVIEW", "READY_FOR_HUMAN_REVIEW" in body, "generated")
    check("draft PR body has candidate SHA", candidate_sha in body, "sha present")

    # 6. Windows sync: BLOCKED when dirty
    (Path(win_repo) / "uncommitted.txt").write_text("dirty")
    gates = check_gates(win_repo, f"agent/{task_id}", candidate_sha, candidate_sha,
                        candidate_sha, True, True)
    check("windows sync blocked when dirty", not gates.windows_clean, "dirty detected")
    os.remove(Path(win_repo) / "uncommitted.txt")

    # 7. Windows sync: BLOCKED when not pushed to remote (candidate not on origin)
    # (the executor pushed to `bare`, windows clone must fetch first)
    git(["fetch", "origin"], cwd=win_repo)
    gates = check_gates(win_repo, f"agent/{task_id}", candidate_sha, candidate_sha,
                        candidate_sha, True, True)
    check("windows gates pass when clean+pushed", gates.all_pass,
          f"clean={gates.windows_clean} pushed={gates.candidate_pushed} ff={gates.fast_forwardable}")

    # 8. Windows sync: actual sync
    final = win_sync(win_repo, f"agent/{task_id}", candidate_sha, candidate_sha,
                     candidate_sha, True, True)
    check("windows sync completed", final == candidate_sha, f"head={final[:12]}")
    win_head = current_sha(win_repo)
    check("windows HEAD == candidate", win_head == candidate_sha, f"{win_head[:12]}")

    # 9. Final state
    store.transition(task_id, TaskState.DRAFT_PR_CREATED)
    store.transition(task_id, TaskState.WINDOWS_SYNCED)
    r = store.transition(task_id, TaskState.READY_FOR_HUMAN_REVIEW)
    check("reached READY_FOR_HUMAN_REVIEW", r.state == TaskState.READY_FOR_HUMAN_REVIEW.value, "done")

    # 10. budget summary
    s = budget.summary(task_id)
    check("codex budget 2/4 used", s["total_used"] == 2, f"{s['total_used']}/4")

    # 11. SHA integrity throughout
    t = store.get(task_id)
    check("SHA integrity: tested==reviewed==candidate",
          t.tested_sha == t.candidate_sha == t.reviewed_sha, f"all={t.candidate_sha[:12]}")

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\nE2E Summary: {passed} PASS, {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
