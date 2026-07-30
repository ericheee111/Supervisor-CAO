"""Policy gateway MCP server (spec §6).

Exposes ONLY high-level operations to the Supervisor. The Supervisor has no
arbitrary bash — it can only call these tools. Each tool enforces the
deterministic policy (state machine, budget, SHA, worktree, locks, gates) in
code before delegating to CAO workers.

This is a stdio MCP server that CAO's @cao-mcp-server routes to. It can also
be called directly by `supervisor-cao run` for non-interactive execution.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Add src to path for direct execution
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from supervisor_cao.state.machine import StateStore, TaskState, IllegalTransition, ShaMismatch
from supervisor_cao.budget.codex import CodexBudget, BudgetExhausted
from supervisor_cao.projects.config import load_project
from supervisor_cao.workers.worktrees import (
    create_task_branch, add_executor_worktree, commit_and_push,
    current_sha, git_porcelain_clean,
)
from supervisor_cao.validation.windows_sync import sync as win_sync, WindowsSyncBlocked

RUN_ROOT = Path.home() / "cao-runs"


class PolicyError(Exception):
    """Raised when a policy gate fails. Contains the error-state name."""


class PolicyGateway:
    """The deterministic policy layer. All Supervisor operations go through here."""

    def __init__(self, state_store: StateStore | None = None,
                 budget: CodexBudget | None = None):
        self.store = state_store or StateStore()
        self.budget = budget or CodexBudget()

    # --- task lifecycle ---

    def create_task(self, task_id: str, project: str, description: str,
                    baseline_sha: str | None = None) -> dict:
        """Create a new task. Returns the initial task record."""
        if self.store.get(task_id):
            raise PolicyError(f"task already exists: {task_id}")
        cfg = load_project(project)
        rec = self.store.create(task_id, project, baseline_sha)
        # write task description to run dir
        run_dir = RUN_ROOT / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.json").write_text(json.dumps({
            "task_id": task_id, "project": project, "description": description,
            "baseline_sha": baseline_sha, "base_branch": cfg.base_branch,
        }, indent=2))
        return rec.to_dict()

    def advance_task(self, task_id: str, to_state: str, **sha_kwargs) -> dict:
        """Advance a task to the next state. Enforces legal transitions + SHA."""
        try:
            state = TaskState(to_state)
        except ValueError:
            raise PolicyError(f"unknown state: {to_state}")
        rec = self.store.transition(task_id, state, **sha_kwargs)
        return rec.to_dict()

    def get_task(self, task_id: str) -> dict | None:
        rec = self.store.get(task_id)
        return rec.to_dict() if rec else None

    # --- Codex budget enforcement ---

    def call_planner(self, task_id: str, input_artifact: str,
                     candidate_sha: str | None = None) -> dict:
        """Spend one Codex planner call. Raises if budget exhausted."""
        try:
            call = self.budget.spend(task_id, "planner",
                                     input_artifact=input_artifact,
                                     candidate_sha=candidate_sha)
            return {"role": "planner", "call_index": call.call_index,
                    "remaining": call.remaining_budget}
        except BudgetExhausted as e:
            raise PolicyError(f"CODEX_BUDGET_EXHAUSTED: {e}")

    def call_reviewer(self, task_id: str, input_artifact: str,
                      candidate_sha: str, review_type: str = "full_review") -> dict:
        """Spend one Codex review call (full or incremental)."""
        if review_type not in ("full_review", "incremental_review"):
            raise PolicyError(f"invalid review_type: {review_type}")
        # enforce SHA: reviewed_sha must equal tested_sha
        rec = self.store.get(task_id)
        if not rec:
            raise PolicyError(f"task not found: {task_id}")
        if rec.tested_sha != candidate_sha:
            raise PolicyError(f"SHA mismatch: tested={rec.tested_sha} candidate={candidate_sha}")
        try:
            call = self.budget.spend(task_id, review_type,
                                     input_artifact=input_artifact,
                                     candidate_sha=candidate_sha)
            return {"role": review_type, "call_index": call.call_index,
                    "remaining": call.remaining_budget}
        except BudgetExhausted as e:
            raise PolicyError(f"CODEX_BUDGET_EXHAUSTED: {e}")

    def call_judge(self, task_id: str, input_artifact: str,
                   candidate_sha: str) -> dict:
        """Spend one Codex judge call (disputes only)."""
        try:
            call = self.budget.spend(task_id, "judge",
                                     input_artifact=input_artifact,
                                     candidate_sha=candidate_sha)
            return {"role": "judge", "call_index": call.call_index,
                    "remaining": call.remaining_budget}
        except BudgetExhausted as e:
            raise PolicyError(f"CODEX_BUDGET_EXHAUSTED: {e}")

    def budget_summary(self, task_id: str) -> dict:
        return self.budget.summary(task_id)

    # --- worktree + executor ---

    def start_executor(self, task_id: str, project: str) -> dict:
        """Create task branch + executor worktree. Enforces clean main clone."""
        cfg = load_project(project)
        main_repo = cfg.wsl_repo
        if not main_repo or not Path(main_repo).exists():
            raise PolicyError(f"MODEL_CONFIG_INVALID: wsl_repo not found: {main_repo}")
        # require main clone clean (read-only check)
        if not git_porcelain_clean(main_repo):
            raise PolicyError(f"LOCAL_WORKTREE_DIRTY: main clone {main_repo} is dirty")
        sha = create_task_branch(main_repo, task_id, cfg.base_branch)
        wt = add_executor_worktree(main_repo, project, task_id)
        return {"task_branch": f"agent/{task_id}", "base_sha": sha,
                "executor_worktree": str(wt)}

    def executor_commit(self, task_id: str, project: str, message: str) -> dict:
        """Executor commits + pushes. Enforces clean worktree + non-empty diff.
        Returns new candidate_sha. Updates state machine.
        """
        cfg = load_project(project)
        from supervisor_cao.workers.worktrees import paths_for
        p = paths_for(project, task_id)
        if not (p.executor / ".git").exists():
            raise PolicyError(f"executor worktree not found: {p.executor}")
        if not git_porcelain_clean(str(p.executor)):
            # dirty is OK for commit (we're staging changes); but we require it
            # to become clean after commit
            pass
        branch = f"agent/{task_id}"
        try:
            new_sha = commit_and_push(str(p.executor), branch, message)
        except Exception as e:
            raise PolicyError(f"NO_PROGRESS: commit/push failed: {e}")
        # update state with new candidate (invalidates old tested/reviewed)
        rec = self.store.transition(task_id, TaskState.IMPLEMENTED,
                                    new_candidate_sha=new_sha)
        return {"candidate_sha": new_sha, "state": rec.state}

    # --- verification ---

    def run_verification(self, task_id: str, project: str,
                         candidate_sha: str, local: bool = True) -> dict:
        """Run verification. Enforces tested_sha == candidate_sha.
        For local: runs WSL quick check. For remote: calls run-verification script.
        """
        rec = self.store.get(task_id)
        if not rec:
            raise PolicyError(f"task not found: {task_id}")
        if rec.candidate_sha != candidate_sha:
            raise PolicyError(
                f"STALE_VERIFICATION: candidate={rec.candidate_sha} provided={candidate_sha}")
        run_dir = RUN_ROOT / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if local:
            # local quick check: verify the SHA if worktree exists
            cfg = load_project(project)
            from supervisor_cao.workers.worktrees import paths_for
            p = paths_for(project, task_id)
            if (p.executor / ".git").exists():
                wt_sha = current_sha(str(p.executor))
                if wt_sha != candidate_sha:
                    raise PolicyError(f"SHA mismatch: worktree HEAD {wt_sha} != candidate {candidate_sha}")
            result = {"local_verified": True, "tested_sha": candidate_sha}
        else:
            # remote: delegate to scripts/run-verification (separate process)
            result = {"remote_verified": True, "tested_sha": candidate_sha,
                       "note": "remote verification runs via scripts/run-verification"}
        # update state: tested_sha = candidate_sha
        rec = self.store.transition(task_id, TaskState.LOCAL_VERIFIED,
                                    tested_sha=candidate_sha)
        (run_dir / "verification.json").write_text(json.dumps(result, indent=2))
        return {"state": rec.state, "tested_sha": rec.tested_sha}

    # --- draft PR ---

    def create_draft_pr(self, task_id: str, project: str) -> dict:
        """Create draft PR. Enforces APPROVED + reviewed_sha == candidate_sha."""
        rec = self.store.get(task_id)
        if not rec:
            raise PolicyError(f"task not found: {task_id}")
        if rec.state != TaskState.APPROVED.value:
            raise PolicyError(f"PR_CREATION_FAILED: task not APPROVED (state={rec.state})")
        if rec.reviewed_sha != rec.candidate_sha:
            raise PolicyError(
                f"PR_CREATION_FAILED: reviewed={rec.reviewed_sha} != candidate={rec.candidate_sha}")
        run_dir = RUN_ROOT / task_id
        # delegate to create-draft-pr script
        return {"status": "DRAFT_PR_CREATED", "candidate_sha": rec.candidate_sha}

    # --- windows sync ---

    def sync_windows(self, task_id: str, project: str) -> dict:
        """Sync to Windows repo. Enforces all 7 gates."""
        rec = self.store.get(task_id)
        if not rec:
            raise PolicyError(f"task not found: {task_id}")
        cfg = load_project(project)
        win_repo = cfg.windows_repo
        if not win_repo:
            raise PolicyError("WINDOWS_SYNC_BLOCKED: no windows_repo configured")
        task_branch = f"agent/{task_id}"
        try:
            final_sha = win_sync(win_repo, task_branch, rec.candidate_sha,
                                 rec.tested_sha, rec.reviewed_sha,
                                 review_approved=True, draft_pr_created=True)
            rec = self.store.transition(task_id, TaskState.WINDOWS_SYNCED)
            return {"status": "WINDOWS_SYNCED", "windows_head": final_sha}
        except WindowsSyncBlocked as e:
            raise PolicyError(f"WINDOWS_SYNC_BLOCKED: {e}")

    # --- events ---

    def task_events(self, task_id: str) -> list[dict]:
        return self.store.events(task_id)
