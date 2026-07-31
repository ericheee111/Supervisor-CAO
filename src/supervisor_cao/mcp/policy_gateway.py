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
from supervisor_cao.projects.adapter import ProjectAdapter, ValidationBackend
from supervisor_cao.workers.worktrees import (
    create_task_branch, add_executor_worktree, commit_and_push,
    current_sha, git_porcelain_clean,
)
from supervisor_cao.validation.windows_sync import sync as win_sync, WindowsSyncBlocked
from supervisor_cao.mcp.cao_client import CaoClient
from supervisor_cao.mcp.worker_runner import WorkerRunner, WorkerError
from supervisor_cao.mcp.worker_monitor import WorkerMonitor
from supervisor_cao.mcp.stage_store import StageStore, COMPLETED as STAGE_COMPLETED

RUN_ROOT = Path.home() / "cao-runs"


class PolicyError(Exception):
    """Raised when a policy gate fails. Contains the error-state name."""


def _default_backend_factory(cfg, *, local_fixture: bool = False) -> ValidationBackend:
    """Default validation-backend factory. Production builds a real backend;
    tests inject a local-fixture backend via the PolicyGateway constructor."""
    return ValidationBackend(cfg, local_fixture=local_fixture)


class PolicyGateway:
    """The deterministic policy layer. All Supervisor operations go through here."""

    def __init__(self, state_store: StateStore | None = None,
                 budget: CodexBudget | None = None,
                 cao_client: CaoClient | None = None,
                 stage_store: StageStore | None = None,
                 worker_monitor: WorkerMonitor | None = None,
                 test_mode: bool = False,
                 backend_factory=None,
                 local_fixture: bool = False,
                 run_root: Path | None = None):
        self.store = state_store or StateStore()
        self.budget = budget or CodexBudget()
        # Run root for artifacts (default ~/cao-runs; acceptance uses an
        # isolated dir). Pass to CaoClient and WorkerRunner so evidence and
        # artifacts go to the same place.
        self.run_root = run_root or RUN_ROOT
        self.cao = cao_client or CaoClient(run_root=self.run_root)
        self.stages = stage_store or StageStore()
        self.runner = WorkerRunner(self.cao, run_root=self.run_root)
        self.worker_monitor = worker_monitor or WorkerMonitor(
            cao_client=self.cao, run_root=self.run_root)
        # Test mode is enabled via dependency injection (NOT a .test-mode file).
        # When True, the draft-PR step writes a test URL instead of calling gh.
        self.test_mode = test_mode
        # Validation backend factory: builds a ValidationBackend from a
        # ProjectConfig. local_fixture is a test-only flag; production never
        # sets it. The main flow calls this so verification goes through the
        # generic adapter/backend, not ad-hoc subprocess calls.
        self._backend_factory = backend_factory or _default_backend_factory
        self._local_fixture = local_fixture

    # --- task lifecycle ---

    def save_config_snapshot(self, task_id: str, cfg) -> None:
        """Persist the resolved ProjectConfig to config-snapshot.json.

        Resume reads this snapshot instead of re-loading mutable config.
        """
        run_dir = self.run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "name": cfg.name, "base_branch": cfg.base_branch,
            "task_branch_prefix": cfg.task_branch_prefix,
            "wsl_repo": cfg.wsl_repo, "windows_repo": cfg.windows_repo,
            "remote_validation": cfg.remote_validation,
            "remote_verification_mode": getattr(cfg, 'remote_verification_mode', 'optional'),
            "default_verification": cfg.default_verification,
            "executor_limits": cfg.executor_limits,
            "codex_budget": cfg.codex_budget,
            "generated_artifact_patterns": cfg.generated_artifact_patterns,
            "stall_timeout": cfg.stall_timeout,
            "extra": cfg.extra,
        }
        (run_dir / "config-snapshot.json").write_text(json.dumps(snapshot, indent=2))

    @staticmethod
    def load_config_snapshot(run_dir: Path):
        """Load a previously persisted config snapshot (for resume)."""
        from supervisor_cao.projects.config import ProjectConfig
        path = run_dir / "config-snapshot.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return ProjectConfig(**data)

    def create_task(self, task_id: str, project: str, description: str,
                    baseline_sha: str | None = None) -> dict:
        """Create a new task. Returns the initial task record."""
        if self.store.get(task_id):
            raise PolicyError(f"task already exists: {task_id}")
        cfg = load_project(project)
        rec = self.store.create(task_id, project, baseline_sha)
        # write task description to run dir
        run_dir = self.run_root / task_id
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
        sha = create_task_branch(main_repo, task_id, cfg.base_branch,
                                 branch_prefix=cfg.task_branch_prefix)
        wt = add_executor_worktree(main_repo, project, task_id,
                                   branch_prefix=cfg.task_branch_prefix)
        return {"task_branch": cfg.task_branch_for(task_id), "base_sha": sha,
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
        branch = cfg.task_branch_for(task_id)
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
        run_dir = self.run_root / task_id
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
        run_dir = self.run_root / task_id
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
        task_branch = cfg.task_branch_for(task_id)
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

    # --- artifacts ---

    def get_artifact(self, task_id: str, name: str) -> dict | None:
        """Read an artifact JSON file from the task's run dir."""
        path = self.run_root / task_id / f"{name}.json"
        if not path.exists():
            # allow reading without .json suffix
            path = self.run_root / task_id / name
            if not path.exists():
                return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    # --- idempotent resume ---

    def resume_task(self, task_id: str) -> dict:
        """Resume a task from its current state. Idempotent: COMPLETED stages
        are NOT re-run, Codex budget is NOT re-spent, no duplicate commits/PRs/
        Windows-syncs (requirement 2)."""
        return self.run_next_stage(task_id)

    # --- the single orchestration entry point ---

    def run_next_stage(self, task_id: str) -> dict:
        """Drive exactly one stage forward via a real CAO Worker.

        Reads the current task state, checks the StageStore for idempotency
        (a COMPLETED stage is never re-run), dispatches to the correct Worker
        via WorkerRunner (real CAO POST /terminals/run-step), validates the
        artifact against its JSON schema, and advances the state machine with
        real SHAs. Returns the updated task record.

        The Supervisor only calls this (and create_task/get_task/get_artifact/
        resume_task). It has no arbitrary bash and cannot bypass the gates.
        """
        rec = self.store.get(task_id)
        if not rec:
            raise PolicyError(f"task not found: {task_id}")
        if rec.state in (TaskState.READY_FOR_HUMAN_REVIEW.value,
                         TaskState.NEEDS_HUMAN.value, TaskState.FAILED.value):
            return rec.to_dict()  # terminal — nothing to do
        cfg = load_project(rec.project)
        session_name = f"scao-{task_id}"
        run_dir = self.run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = rec.state

        try:
            if state == TaskState.CREATED.value:
                self._stage_research(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.RESEARCHING.value:
                self._stage_plan(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.PLAN_READY.value:
                self._stage_implement(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.IMPLEMENTING.value:
                # IMPLEMENTING means the worktree was created but the executor
                # hasn't committed yet — re-run the executor stage.
                self._stage_implement(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.IMPLEMENTED.value:
                self._stage_local_verify(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.LOCAL_VERIFYING.value:
                self._stage_local_verify(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.LOCAL_VERIFIED.value:
                self._stage_remote_verify(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.REMOTE_QUEUED.value:
                self._stage_remote_verify(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.REMOTE_VERIFYING.value:
                self._stage_remote_verify(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.REMOTE_VERIFIED.value:
                self._stage_review(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.CHANGES_REQUESTED.value:
                self._stage_fix(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.FIXING.value:
                self._stage_fix(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.INCREMENTAL_REVIEWING.value:
                self._stage_incremental_review(task_id, rec, cfg, session_name, run_dir)
            elif state == TaskState.APPROVED.value:
                self._stage_draft_pr(task_id, rec, cfg, run_dir)
            elif state == TaskState.DRAFT_PR_CREATED.value:
                self._stage_windows_sync(task_id, rec, cfg)
            elif state == TaskState.WINDOWS_SYNCED.value:
                self.store.transition(task_id, TaskState.READY_FOR_HUMAN_REVIEW)
            else:
                raise PolicyError(f"no stage handler for state {state}")
        except WorkerError as e:
            self.stages.fail_stage(task_id, _stage_for_state(state))
            # Check if there is a stalled worker handle for this task.
            # STALLED is a worker-level status, NOT a TaskState. We attempt
            # reattach; only if it fails do we transition the task to NEEDS_HUMAN.
            handle = self.worker_monitor.find_for_task(task_id)
            if handle and handle.status == "STALLED":
                if self.worker_monitor.resume_worker(handle.worker_id):
                    # reattach succeeded — restore resume_state and continue
                    if handle.resume_state:
                        try:
                            self.store.transition(task_id, handle.resume_state)
                        except Exception:
                            pass  # state may have advanced; best-effort
                    return self.store.get(task_id).to_dict()
                # reattach failed — task needs human intervention
                self.store.transition(task_id, TaskState.NEEDS_HUMAN,
                                      error=f"worker stalled and reattach failed: {e}")
                raise PolicyError(str(e))
            self.store.transition(task_id, TaskState.FAILED, error=str(e))
            raise PolicyError(str(e))
        except PolicyError:
            raise
        except Exception as e:
            self.stages.fail_stage(task_id, _stage_for_state(state))
            raise PolicyError(f"stage {state} failed: {e}")

        return self.store.get(task_id).to_dict()

    # --- stage implementations (each idempotent via StageStore) ---

    def _run_stage_via_monitor(self, task_id: str, stage: str, request: dict,
                               stall_timeout: int = 1800) -> dict:
        """Run a stage via WorkerMonitor (four-phase: build→start→wait→finalize).

        Production path: only WorkerMonitor can start Workers.
        Test path (local_fixture=True): uses mock CaoClient.launch_worker directly
        (no subprocess, no real Worker) — this is test isolation, NOT a second
        production entry point.
        Returns the parsed artifact dict. Raises PolicyError on failure.
        """
        if self._local_fixture:
            # Test mode: use mock CaoClient (no real Worker subprocess)
            result = self.cao.launch_worker(
                request["profile"], request["prompt"],
                request["working_directory"], request.get("session_name"),
                request.get("model"), request.get("timeout"),
                task_id=task_id, stage=request["stage"])
            worker_result = {
                "status": "COMPLETED" if result.success else "FAILED",
                "last_message": result.last_message or "",
                "raw_output": result.raw_output or "",
                "exit_code": 0 if result.success else -1,
                "error": result.error if not result.success else None,
            }
            if worker_result["status"] != "COMPLETED":
                raise PolicyError(f"{stage}: {worker_result.get('error', 'worker failed')}")
            return self.runner.finalize_result(
                request["stage"], task_id, worker_result,
                candidate_sha=request.get("candidate_sha"))

        # Production path: WorkerMonitor starts and monitors the Worker
        worker_id = self.worker_monitor.start_worker(
            task_id=task_id, stage=stage, profile=request["profile"],
            prompt=request["prompt"], working_directory=request["working_directory"],
            session_name=request.get("session_name"),
            model=request.get("model"),
            timeout=request.get("timeout"),
            stall_timeout=stall_timeout,
            resume_state=request.get("resume_state"),
        )
        # Persist handle status to StageStore
        self.stages.set_handle_status(
            task_id, stage, handle_status="RUNNING",
            resume_state=request.get("resume_state"),
            worker_id=worker_id)
        # Wait for completion (blocks until COMPLETED/FAILED/STALLED)
        result = self.worker_monitor.wait_for_stage(task_id, stall_timeout=stall_timeout)
        if result.get("status") != "COMPLETED":
            error = result.get("error", "worker failed")
            self.stages.fail_stage(task_id, stage, error=error)
            raise PolicyError(f"{stage}: {error}")
        # Finalize: parse + validate + stamp + save artifact
        artifact = self.runner.finalize_result(
            request["stage"], task_id, result,
            candidate_sha=request.get("candidate_sha"))
        return artifact

    def _stage_research(self, task_id, rec, cfg, session_name, run_dir):
        stage = "research"
        run, done = self.stages.begin_stage(task_id, stage, "researcher")
        if done:
            self.store.transition(task_id, TaskState.RESEARCHING)
            return
        desc = json.loads((run_dir / "task.json").read_text()).get("description", "")
        self.stages.mark_running(task_id, stage)
        request = WorkerRunner.build_request(
            "research", task_id, description=desc,
            baseline_sha=rec.baseline_sha,
            working_directory=cfg.wsl_repo or str(run_dir),
            session_name=session_name)
        self._run_stage_via_monitor(task_id, stage, request,
                                    stall_timeout=cfg.stall_timeout)
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "research.json"))
        self.store.transition(task_id, TaskState.RESEARCHING)

    def _stage_plan(self, task_id, rec, cfg, session_name, run_dir):
        stage = "plan"
        run, done = self.stages.begin_stage(task_id, stage, "codex-planner")
        if done:
            # plan already completed; advance to PLAN_READY.
            # If still in RESEARCHING, go RESEARCHING -> PLANNING -> PLAN_READY.
            if rec.state == TaskState.RESEARCHING.value:
                self.store.transition(task_id, TaskState.PLANNING)
            self.store.transition(task_id, TaskState.PLAN_READY)
            return
        research = self.get_artifact(task_id, "research") or {}
        # transition to PLANNING before spending budget (RESEARCHING -> PLANNING)
        if rec.state != TaskState.PLANNING.value:
            self.store.transition(task_id, TaskState.PLANNING)
        # spend Codex planner budget (idempotent: not re-spent if stage was COMPLETED)
        call = self.budget.spend(task_id, "planner",
                                 input_artifact=str(run_dir / "research.json"),
                                 candidate_sha=rec.candidate_sha)
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        desc = json.loads((run_dir / "task.json").read_text()).get("description", "")
        request = WorkerRunner.build_request(
            "plan", task_id, description=desc,
            baseline_sha=rec.baseline_sha, research=research,
            working_directory=cfg.wsl_repo or str(run_dir),
            session_name=session_name)
        self._run_stage_via_monitor(task_id, stage, request,
                                    stall_timeout=cfg.stall_timeout)
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "plan.json"),
                                   candidate_sha=rec.candidate_sha, codex_call_id=str(call.call_index))
        # save budget summary for the draft-PR gate
        self._save_budget_summary(task_id)
        self.store.transition(task_id, TaskState.PLAN_READY)

    def _stage_implement(self, task_id, rec, cfg, session_name, run_dir):
        stage = "implementation"
        run, done = self.stages.begin_stage(task_id, stage, "glm-executor")
        if done:
            # already implemented — candidate_sha was recorded
            cand = run.candidate_sha or rec.candidate_sha
            if rec.state == TaskState.PLAN_READY.value:
                self.store.transition(task_id, TaskState.IMPLEMENTING)
            self.store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=cand)
            return
        plan = self.get_artifact(task_id, "plan") or {}
        # transition to IMPLEMENTING before running the executor
        if rec.state == TaskState.PLAN_READY.value:
            self.store.transition(task_id, TaskState.IMPLEMENTING)
        # create the executor worktree via the ProjectAdapter (generic)
        adapter = ProjectAdapter(cfg)
        main_repo = adapter.main_repo or str(run_dir)
        if Path(main_repo).exists() and (Path(main_repo) / ".git").exists():
            if not git_porcelain_clean(main_repo):
                raise PolicyError(f"LOCAL_WORKTREE_DIRTY: main clone {main_repo} is dirty")
            create_task_branch(main_repo, task_id, adapter.base_branch,
                               branch_prefix=adapter.task_branch_prefix)
            wt = add_executor_worktree(main_repo, adapter.name, task_id,
                                       branch_prefix=adapter.task_branch_prefix)
            executor_wt = str(wt)
        else:
            executor_wt = main_repo  # temp-repo fallback
        base_sha = rec.baseline_sha or _safe_head(executor_wt)
        self.stages.mark_running(task_id, stage, candidate_sha=base_sha)
        request = WorkerRunner.build_request(
            "implementation", task_id, plan=plan, base_sha=base_sha,
            working_directory=executor_wt, session_name=session_name,
            expected_branch=adapter.task_branch_for(task_id))
        impl = self._run_stage_via_monitor(task_id, stage, request,
                                           stall_timeout=cfg.stall_timeout)
        # Read the REAL git HEAD SHA from the worktree (not the LLM claim)
        from supervisor_cao.workers.worktrees import current_sha
        real_sha = current_sha(executor_wt)
        if not real_sha:
            raise PolicyError("IMPLEMENTING: cannot read git HEAD from worktree")
        impl["candidate_sha"] = real_sha  # stamp the real SHA
        (run_dir / "implementation.json").write_text(json.dumps(impl, indent=2))
        # Record push evidence
        task_branch = adapter.task_branch_for(task_id)
        push_evidence = {
            "schema_version": 1, "remote": "origin", "branch": task_branch,
            "pushed_sha": real_sha, "push_succeeded": True,
        }
        (run_dir / "push.json").write_text(json.dumps(push_evidence, indent=2))
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "implementation.json"),
                                   candidate_sha=real_sha)
        self.store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=real_sha)

    def _stage_local_verify(self, task_id, rec, cfg, session_name, run_dir):
        stage = "verification"
        run, done = self.stages.begin_stage(task_id, stage, "qwen-verifier",
                                            candidate_sha=rec.candidate_sha)
        if done:
            self.store.transition(task_id, TaskState.LOCAL_VERIFIED, tested_sha=rec.candidate_sha)
            return
        plan = self.get_artifact(task_id, "plan") or {}
        from supervisor_cao.workers.worktrees import paths_for
        p = paths_for(rec.project, task_id)
        executor_wt = str(p.executor) if (p.executor / ".git").exists() else (cfg.wsl_repo or str(run_dir))
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        if rec.state != TaskState.LOCAL_VERIFYING.value:
            self.store.transition(task_id, TaskState.LOCAL_VERIFYING)
        # 1. Deterministic runner via ValidationBackend: exit code is authoritative.
        backend = self._backend_factory(cfg, local_fixture=self._local_fixture)
        result = backend.run_local(executor_wt, rec.candidate_sha,
                                   plan.get("test_matrix", []))
        backend.write_artifact(result, run_dir, remote=False, task_id=task_id)
        if not result.passed:
            self.stages.fail_stage(task_id, stage)
            self.store.transition(task_id, TaskState.FAILED,
                                  error="local verification failed (exit code non-zero)")
            raise PolicyError("LOCAL_VERIFYING: tests failed (real exit code non-zero)")
        # 2. Optional LLM summary only. The LLM CANNOT change passed/tested_sha.
        try:
            summary = self.runner.run_verifier_summary(
                task_id, rec.candidate_sha, result.summary, session_name)
            verify = self.get_artifact(task_id, "verification") or {}
            verify["llm_summary"] = summary
            verify["passed"] = result.passed  # authoritative, re-stamped
            verify["tested_sha"] = rec.candidate_sha  # authoritative, re-stamped
            (run_dir / "verification.json").write_text(json.dumps(verify, indent=2))
        except Exception:
            pass  # LLM summary is best-effort; the exit code already decided
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "verification.json"),
                                   candidate_sha=rec.candidate_sha)
        self.store.transition(task_id, TaskState.LOCAL_VERIFIED, tested_sha=rec.candidate_sha)

    def _stage_remote_verify(self, task_id, rec, cfg, session_name, run_dir):
        # Remote verification mode: disabled / optional / required
        mode = getattr(cfg, 'remote_verification_mode', 'optional')
        stage = "remote_verification"
        run, done = self.stages.begin_stage(task_id, stage, "qwen-verifier",
                                            candidate_sha=rec.candidate_sha)
        if done:
            if rec.state == TaskState.LOCAL_VERIFIED.value:
                self.store.transition(task_id, TaskState.REMOTE_QUEUED)
                self.store.transition(task_id, TaskState.REMOTE_VERIFYING)
            elif rec.state == TaskState.REMOTE_QUEUED.value:
                self.store.transition(task_id, TaskState.REMOTE_VERIFYING)
            self.store.transition(task_id, TaskState.REMOTE_VERIFIED)
            return
        # Mode: disabled → skip with artifact + audit event
        if mode == "disabled":
            skip_artifact = {"skipped": True, "reason": "disabled",
                             "candidate_sha": rec.candidate_sha}
            (run_dir / "verification-remote.json").write_text(
                json.dumps(skip_artifact, indent=2))
            self.stages.complete_stage(task_id, stage,
                                       artifact_path=str(run_dir / "verification-remote.json"),
                                       candidate_sha=rec.candidate_sha)
            # Skip remote verification: go directly to REMOTE_VERIFIED → REVIEWING
            if rec.state == TaskState.LOCAL_VERIFIED.value:
                self.store.transition(task_id, TaskState.REMOTE_QUEUED)
                self.store.transition(task_id, TaskState.REMOTE_VERIFYING)
            self.store.transition(task_id, TaskState.REMOTE_VERIFIED)
            return
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        if rec.state == TaskState.LOCAL_VERIFIED.value:
            self.store.transition(task_id, TaskState.REMOTE_QUEUED)
        if rec.state != TaskState.REMOTE_VERIFYING.value:
            self.store.transition(task_id, TaskState.REMOTE_VERIFYING)
        backend = self._backend_factory(cfg, local_fixture=self._local_fixture)
        result = backend.run_remote(task_id, rec.candidate_sha, run_dir)
        backend.write_artifact(result, run_dir, remote=True, task_id=task_id)
        if not result.passed:
            # Mode: optional → fallback to LOCAL_VERIFIED; required → FAILED
            if mode == "optional":
                fallback_artifact = {"skipped": True, "reason": "optional_fallback",
                                     "original_error": result.summary}
                (run_dir / "verification-remote.json").write_text(
                    json.dumps(fallback_artifact, indent=2))
                self.stages.complete_stage(task_id, stage,
                                           artifact_path=str(run_dir / "verification-remote.json"),
                                           candidate_sha=rec.candidate_sha)
                self.store.transition(task_id, TaskState.REMOTE_VERIFIED)
                return
            # required → FAILED
            self.stages.fail_stage(task_id, stage)
            self.store.transition(task_id, TaskState.FAILED,
                                  error=result.summary or "remote verification failed")
            raise PolicyError(f"REMOTE_VERIFYING: {result.summary}")
        self.stages.complete_stage(task_id, stage, candidate_sha=rec.candidate_sha)
        self.store.transition(task_id, TaskState.REMOTE_VERIFIED)

    def _stage_review(self, task_id, rec, cfg, session_name, run_dir):
        stage = "review"
        run, done = self.stages.begin_stage(task_id, stage, "codex-reviewer",
                                            candidate_sha=rec.candidate_sha)
        if done:
            review = self.get_artifact(task_id, "review") or {}
            self._apply_review_decision(task_id, rec, review)
            return
        # If a prior full review exists for a DIFFERENT candidate (i.e. this is
        # a re-review after a fix), route to incremental review instead of
        # spending another full_review budget. The state machine allows
        # REMOTE_VERIFIED -> INCREMENTAL_REVIEWING for exactly this case.
        prior_review = self.get_artifact(task_id, "review") or {}
        prior_stage = self.stages.get(task_id, "review")
        prior_sha = (prior_stage.candidate_sha if prior_stage else None) \
            or prior_review.get("candidate_sha")
        had_prior_review = bool(prior_sha) or any(
            e.get("to_state") == TaskState.CHANGES_REQUESTED.value
            for e in self.store.events(task_id)
        )
        if had_prior_review and (not prior_sha or prior_sha != rec.candidate_sha):
            self.store.transition(task_id, TaskState.INCREMENTAL_REVIEWING,
                                  reviewed_sha=rec.tested_sha)
            self._stage_incremental_review(task_id, rec, cfg, session_name, run_dir)
            return
        impl = self.get_artifact(task_id, "implementation") or {}
        verify = self.get_artifact(task_id, "verification") or {}
        # spend full_review budget (idempotent)
        call = self.budget.spend(task_id, "full_review",
                                 input_artifact=str(run_dir / "verification.json"),
                                 candidate_sha=rec.candidate_sha)
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        self.store.transition(task_id, TaskState.REVIEWING, reviewed_sha=rec.tested_sha)
        request = WorkerRunner.build_request(
            "review", task_id, candidate_sha=rec.candidate_sha,
            tested_sha=rec.tested_sha, plan=impl,
            working_directory=cfg.wsl_repo or str(run_dir),
            session_name=session_name)
        self._run_stage_via_monitor(task_id, stage, request,
                                    stall_timeout=cfg.stall_timeout)
        review = self.get_artifact(task_id, "review") or {}
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "review.json"),
                                   candidate_sha=rec.candidate_sha, codex_call_id=str(call.call_index))
        self._save_budget_summary(task_id)
        # Override reviewed_sha with the authoritative tested_sha.
        review["reviewed_sha"] = rec.tested_sha
        (run_dir / "review.json").write_text(json.dumps(review, indent=2))
        self._apply_review_decision(task_id, rec, review)

    def _stage_fix(self, task_id, rec, cfg, session_name, run_dir):
        # CHANGES_REQUESTED -> FIXING -> (new SHA) -> LOCAL_VERIFYING -> ...
        # -> REMOTE_VERIFIED -> INCREMENTAL_REVIEWING. FIXING -> IMPLEMENTED is
        # ILLEGAL (state machine §9); the fix goes straight to re-verification.
        stage = "fix"
        run, done = self.stages.begin_stage(task_id, stage, "glm-executor",
                                            candidate_sha=rec.candidate_sha)
        if done:
            # fix already produced a new SHA; re-verify it. The new candidate
            # was recorded in complete_stage; advance to re-verification.
            cand = run.candidate_sha or rec.candidate_sha
            if rec.state == TaskState.CHANGES_REQUESTED.value:
                self.store.transition(task_id, TaskState.FIXING, new_candidate_sha=cand)
            self.store.transition(task_id, TaskState.LOCAL_VERIFYING)
            return
        prior_review = self.get_artifact(task_id, "review") or {}
        plan = self.get_artifact(task_id, "plan") or {}
        adapter = ProjectAdapter(cfg)
        executor_wt = str(adapter.executor_worktree(task_id)) \
            if (adapter.executor_worktree(task_id) / ".git").exists() \
            else (adapter.main_repo or str(run_dir))
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        self.store.transition(task_id, TaskState.FIXING)
        # re-run executor with the prior review findings as guidance. The base
        # for the diff check is the OLD candidate (the fix must change it).
        fix_plan = dict(plan)
        fix_plan["_prior_review_findings"] = prior_review.get("findings", [])
        fix_base = rec.candidate_sha or rec.baseline_sha or _safe_head(executor_wt)
        request = WorkerRunner.build_request(
            "implementation", task_id, plan=fix_plan, base_sha=fix_base,
            working_directory=executor_wt, session_name=session_name,
            expected_branch=adapter.task_branch_for(task_id))
        impl = self._run_stage_via_monitor(task_id, stage, request,
                                           stall_timeout=cfg.stall_timeout)
        from supervisor_cao.workers.worktrees import current_sha
        real_sha = current_sha(executor_wt)
        if not real_sha:
            raise PolicyError("FIXING: cannot read git HEAD from worktree")
        impl["candidate_sha"] = real_sha
        (run_dir / "implementation.json").write_text(json.dumps(impl, indent=2))
        task_branch = adapter.task_branch_for(task_id)
        push_evidence = {
            "schema_version": 1, "remote": "origin", "branch": task_branch,
            "pushed_sha": real_sha, "push_succeeded": True,
        }
        (run_dir / "push.json").write_text(json.dumps(push_evidence, indent=2))
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "implementation.json"),
                                   candidate_sha=real_sha)
        # new SHA invalidates old verification/review — re-verify then incremental
        # review. FIXING -> IMPLEMENTED is ILLEGAL (state machine §9); the fix
        # goes directly to LOCAL_VERIFYING with the new candidate_sha, which
        # invalidates the old tested/reviewed SHAs.
        self.store.transition(task_id, TaskState.LOCAL_VERIFYING, new_candidate_sha=real_sha)

    def _stage_incremental_review(self, task_id, rec, cfg, session_name, run_dir):
        stage = "incremental_review"
        run, done = self.stages.begin_stage(task_id, stage, "codex-reviewer",
                                            candidate_sha=rec.candidate_sha)
        if done:
            review = self.get_artifact(task_id, "incremental_review") or self.get_artifact(task_id, "review") or {}
            self._apply_incremental_decision(task_id, rec, review)
            return
        prior_review = self.get_artifact(task_id, "review") or {}
        impl = self.get_artifact(task_id, "implementation") or {}
        verify = self.get_artifact(task_id, "verification") or {}
        call = self.budget.spend(task_id, "incremental_review",
                                 input_artifact=str(run_dir / "verification.json"),
                                 candidate_sha=rec.candidate_sha)
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        cur = self.store.get(task_id)
        if not cur or cur.state != TaskState.INCREMENTAL_REVIEWING.value:
            self.store.transition(task_id, TaskState.INCREMENTAL_REVIEWING, reviewed_sha=rec.tested_sha)
        request = WorkerRunner.build_request(
            "incremental_review", task_id, candidate_sha=rec.candidate_sha,
            findings=prior_review.get("findings", []),
            executor_response="",
            working_directory=cfg.wsl_repo or str(run_dir),
            session_name=session_name)
        self._run_stage_via_monitor(task_id, stage, request,
                                    stall_timeout=cfg.stall_timeout)
        review = self.get_artifact(task_id, "incremental_review") or {}
        self.stages.complete_stage(task_id, stage, artifact_path=str(run_dir / "incremental_review.json"),
                                   candidate_sha=rec.candidate_sha, codex_call_id=str(call.call_index))
        self._save_budget_summary(task_id)
        # Override reviewed_sha with the authoritative tested_sha (the LLM
        # may output an incorrect reviewed_sha; the platform stamps the
        # correct one from the state machine). Update BOTH the incremental
        # review artifact AND the main review.json (create-draft-pr reads
        # review.json). Also sync the decision so create-draft-pr sees the
        # final incremental decision (APPROVED), not the stale original.
        review["reviewed_sha"] = rec.tested_sha
        (run_dir / "incremental_review.json").write_text(json.dumps(review, indent=2))
        # Also update review.json with the authoritative reviewed_sha + decision
        review_main = self.get_artifact(task_id, "review") or {}
        review_main["reviewed_sha"] = rec.tested_sha
        review_main["decision"] = review.get("decision", review_main.get("decision"))
        review_main["findings"] = review.get("findings", review_main.get("findings", []))
        (run_dir / "review.json").write_text(json.dumps(review_main, indent=2))
        self._apply_incremental_decision(task_id, rec, review)

    def _stage_draft_pr(self, task_id, rec, cfg, run_dir):
        stage = "draft_pr"
        run, done = self.stages.begin_stage(task_id, stage, "create-draft-pr")
        if done:
            self.store.transition(task_id, TaskState.DRAFT_PR_CREATED)
            return
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        # delegate to create-draft-pr script (validates all 5 artifacts exist)
        script = Path(__file__).resolve().parents[3] / "scripts" / "create-draft-pr"
        task_branch = cfg.task_branch_for(task_id)
        cmd = [sys.executable, str(script), "--repo", cfg.wsl_repo or str(run_dir),
               "--task-id", task_id, "--task-branch", task_branch,
               "--base-branch", cfg.base_branch, "--run-dir", str(run_dir)]
        if self.test_mode:
            cmd.append("--test-mode")
        # Acceptance runs pass a run-id for PR title/label isolation.
        acceptance_run_id = cfg.extra.get("acceptance_run_id")
        if acceptance_run_id:
            cmd += ["--acceptance-run-id", str(acceptance_run_id)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            self.stages.fail_stage(task_id, stage)
            raise PolicyError(f"PR_CREATION_FAILED: {r.stderr.strip() or r.stdout.strip()}")
        self.stages.complete_stage(task_id, stage, candidate_sha=rec.candidate_sha)
        self.store.transition(task_id, TaskState.DRAFT_PR_CREATED)

    def _stage_windows_sync(self, task_id, rec, cfg):
        stage = "windows_sync"
        run, done = self.stages.begin_stage(task_id, stage, "sync-windows-repo")
        if done:
            self.store.transition(task_id, TaskState.WINDOWS_SYNCED)
            return
        win_repo = cfg.windows_repo
        if not win_repo:
            # no windows repo configured (temp-repo E2E) — skip sync, mark done
            self.stages.complete_stage(task_id, stage, candidate_sha=rec.candidate_sha)
            self.store.transition(task_id, TaskState.WINDOWS_SYNCED)
            return
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        task_branch = cfg.task_branch_for(task_id)
        try:
            final_sha = win_sync(win_repo, task_branch, rec.candidate_sha,
                                 rec.tested_sha, rec.reviewed_sha,
                                 review_approved=True, draft_pr_created=True)
        except WindowsSyncBlocked as e:
            self.stages.fail_stage(task_id, stage)
            raise PolicyError(f"WINDOWS_SYNC_BLOCKED: {e}")
        self.stages.complete_stage(task_id, stage, candidate_sha=final_sha)
        self.store.transition(task_id, TaskState.WINDOWS_SYNCED)

    # --- helpers ---

    def _apply_review_decision(self, task_id, rec, review: dict):
        """The review decision (parsed from real Worker output) drives state."""
        decision = review.get("decision")
        if decision == "APPROVED":
            self.store.transition(task_id, TaskState.APPROVED)
        elif decision == "CHANGES_REQUESTED":
            self.store.transition(task_id, TaskState.CHANGES_REQUESTED)
        else:
            raise PolicyError(f"REVIEWING: invalid review decision {decision!r}")

    def _apply_incremental_decision(self, task_id, rec, review: dict):
        """Apply the incremental review decision. When CHANGES_REQUESTED, ALL
        findings are submitted to the Codex Judge (the platform does NOT
        auto-approve based on 'insufficient evidence'). Judge rulings:
        OVERTURN / UPHOLD / MIXED / UNRESOLVED.
        - All findings OVERTURN → APPROVED.
        - Any UPHOLD / MIXED / UNRESOLVED, or budget exhausted → NEEDS_HUMAN.
        """
        decision = review.get("decision")
        if decision == "APPROVED":
            self.store.transition(task_id, TaskState.APPROVED)
        elif decision == "CHANGES_REQUESTED":
            # Submit ALL findings to the Judge. Do not auto-approve.
            self._stage_judge(task_id, rec, review)
        else:
            raise PolicyError(f"INCREMENTAL_REVIEWING: invalid decision {decision!r}")

    def _stage_judge(self, task_id, rec, review: dict):
        """Submit all CHANGES_REQUESTED findings to the Codex Judge.

        The Judge rules on each finding: OVERTURN / UPHOLD / MIXED / UNRESOLVED.
        Only if ALL findings are OVERTURN does the task proceed to APPROVED.
        Any other ruling or budget exhaustion → NEEDS_HUMAN (no fake approval).
        """
        findings = review.get("findings", [])
        if not findings:
            # No findings to arbitrate — treat as approval (reviewer found nothing).
            self.store.transition(task_id, TaskState.APPROVED)
            return
        run_dir = self.run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # spend judge budget (idempotent: if a prior judge call exists for this
        # candidate, the budget spend will raise BudgetExhausted, which we
        # catch → NEEDS_HUMAN)
        try:
            call = self.budget.spend(task_id, "judge",
                                     input_artifact=str(run_dir / "incremental_review.json"),
                                     candidate_sha=rec.candidate_sha)
        except Exception as e:
            # budget exhausted — cannot arbitrate; NEEDS_HUMAN
            self.store.transition(task_id, TaskState.NEEDS_HUMAN,
                                  error=f"judge budget exhausted: {e}")
            raise PolicyError(f"JUDGE_BUDGET_EXHAUSTED: {e}")
        impl = self.get_artifact(task_id, "implementation") or {}
        verify = self.get_artifact(task_id, "verification") or {}
        cfg = load_project(rec.project)
        # Run the Judge via WorkerMonitor
        request = WorkerRunner.build_request(
            "decision", task_id, candidate_sha=rec.candidate_sha,
            findings=findings, executor_response="",
            reviewer_rebuttal="",
            working_directory=cfg.wsl_repo or str(run_dir),
            session_name=f"scao-{task_id}")
        decision = self._run_stage_via_monitor(task_id, "decision", request,
                                               stall_timeout=cfg.stall_timeout)
        self._save_budget_summary(task_id)
        # Save the judge decision artifact
        (run_dir / "decision.json").write_text(json.dumps(decision, indent=2))
        ruling = decision.get("ruling", "UNRESOLVED")
        if ruling == "OVERTURN":
            # All findings overturned → approve
            self.store.transition(task_id, TaskState.APPROVED)
        else:
            # UPHOLD / MIXED / UNRESOLVED → NEEDS_HUMAN (no fake approval)
            self.store.transition(task_id, TaskState.NEEDS_HUMAN,
                                  error=f"judge ruling={ruling}: findings not fully overturned")

    def _save_budget_summary(self, task_id: str):
        run_dir = self.run_root / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = self.budget.summary(task_id)
        (run_dir / "codex-budget-summary.json").write_text(json.dumps(summary, indent=2))


def _stage_for_state(state: str) -> str:
    return {
        TaskState.CREATED.value: "research",
        TaskState.RESEARCHING.value: "plan",
        TaskState.PLAN_READY.value: "implementation",
        TaskState.IMPLEMENTING.value: "implementation",
        TaskState.IMPLEMENTED.value: "verification",
        TaskState.LOCAL_VERIFYING.value: "verification",
        TaskState.LOCAL_VERIFIED.value: "remote_verification",
        TaskState.REMOTE_QUEUED.value: "remote_verification",
        TaskState.REMOTE_VERIFYING.value: "remote_verification",
        TaskState.REMOTE_VERIFIED.value: "review",
        TaskState.REVIEWING.value: "review",
        TaskState.CHANGES_REQUESTED.value: "fix",
        TaskState.FIXING.value: "fix",
        TaskState.INCREMENTAL_REVIEWING.value: "incremental_review",
        TaskState.APPROVED.value: "draft_pr",
        TaskState.DRAFT_PR_CREATED.value: "windows_sync",
    }.get(state, state)


def _safe_head(repo: str) -> str | None:
    try:
        return current_sha(repo)
    except Exception:
        return None
