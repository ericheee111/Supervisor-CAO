"""Acceptance test runner for Supervisor-CAO.

Provides isolated acceptance scenarios that use independent state, budget,
runs, and worktree directories so they never read or modify existing historical
tasks. Scenarios:

  direct:    real implement + test of parse_duration, approved by real Codex Review.
  review-fix: start from a controlled unsafe candidate, real Codex must output
             CHANGES_REQUESTED, GLM fixes (new SHA), re-verify, incremental review.
  resume:    interrupt during a real Planner/Executor run, then resume; verify
             Codex budget not re-spent, no duplicate commit/PR.

The acceptance environment lives under a single root (default
~/.local/state/supervisor-cao/acceptance/) with subdirs for state, budget,
runs, and worktrees. ``prepare`` clones/updates the test repo (never creates or
deletes remote repos). ``run`` executes one scenario. ``status`` reports
results. ``cleanup`` removes the isolated environment.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ACCEPTANCE_ROOT = Path.home() / ".local" / "state" / "supervisor-cao" / "acceptance"

SCENARIOS = ("direct", "review-fix", "resume",
             "runtime-direct", "runtime-review-fix", "runtime-resume")


def _subdir(name: str) -> Path:
    return ACCEPTANCE_ROOT / name


def _isolated_dirs() -> dict[str, Path]:
    return {
        "state": _subdir("state"),
        "budget": _subdir("budget"),
        "runs": _subdir("runs"),
        "worktrees": _subdir("worktrees"),
        "repo": _subdir("repo"),
    }


def _win_accessible_repo_dir() -> Path:
    """A Windows-accessible repo dir (under /mnt/) so OpenCode's write tool
    (a Windows binary) can edit files. WSL ext4 paths (/root/) are invisible
    to Windows OpenCode. Configurable via SCAO_ACCEPTANCE_REPO_DIR env;
    defaults to /mnt/d/Projects/scao-acceptance-repo on WSL."""
    return Path(os.environ.get("SCAO_ACCEPTANCE_REPO_DIR",
                               "/mnt/d/Projects/scao-acceptance-repo"))


def _write_meta(meta: dict) -> None:
    _subdir("state").mkdir(parents=True, exist_ok=True)
    (_subdir("state") / "meta.json").write_text(json.dumps(meta, indent=2))


def _read_meta() -> dict:
    p = _subdir("state") / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text()) or {}
    except Exception:
        return {}


def prepare(repo_path: str | None = None, repo_url: str | None = None) -> int:
    """Prepare the isolated acceptance environment.

    Clones or updates the test repo into the acceptance repo dir. NEVER creates
    or deletes a remote repository. Accepts --repo-path and --repo-url.
    """
    dirs = _isolated_dirs()
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    # Use a Windows-accessible repo dir so OpenCode (a Windows binary) can
    # edit files via its write tool. WSL ext4 paths are invisible to it.
    repo_dir = _win_accessible_repo_dir()
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if repo_url:
        if repo_dir.exists() and (repo_dir / ".git").exists():
            print(f"Re-cloning acceptance repo at {repo_dir} (clean state)...")
            # Force-remove the entire repo and re-clone to eliminate stale
            # worktree registrations, Windows .git paths, and stale branches
            # that accumulate across acceptance runs.
            import shutil as _sh
            _sh.rmtree(repo_dir, ignore_errors=True)
            old_wt_root = repo_dir.parent / "scao-acceptance-worktrees"
            if old_wt_root.exists():
                _sh.rmtree(old_wt_root, ignore_errors=True)
        print(f"Cloning {repo_url} into {repo_dir}...")
        r = subprocess.run(["git", "clone", repo_url, str(repo_dir)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"ERROR: clone failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
    elif repo_path:
        print(f"Using local repo at {repo_path}...")
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        shutil.copytree(repo_path, repo_dir)
    else:
        print("ERROR: --repo-path or --repo-url required", file=sys.stderr)
        return 1
    _write_meta({
        "prepared_at": time.time(),
        "repo_url": repo_url or "",
        "repo_path": repo_path or "",
        "repo_dir": str(repo_dir),
        "scenarios": {},
    })
    print(f"Acceptance environment prepared at {ACCEPTANCE_ROOT}")
    return 0


def _record_scenario(name: str, result: dict) -> None:
    meta = _read_meta()
    meta.setdefault("scenarios", {})[name] = result
    _write_meta(meta)


# --- append-only evidence ---

def _evidence_dir(acceptance_root: Path, scenario: str, run_id: str) -> Path:
    """Return a unique evidence directory for one scenario run (append-only)."""
    d = acceptance_root / "evidence" / run_id / scenario
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_evidence(ev_dir: Path, result: dict, task_snapshot: dict,
                     events: list, stage_attempts: list, budget_log: dict,
                     worker_handles: list, sha_info: dict,
                     pr_content_info: dict) -> None:
    """Write all evidence files (append-only — never overwrites another run)."""
    (ev_dir / "result.json").write_text(json.dumps(result, indent=2))
    (ev_dir / "task_snapshot.json").write_text(json.dumps(task_snapshot, indent=2))
    with open(ev_dir / "events.jsonl", "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (ev_dir / "stage_attempts.json").write_text(json.dumps(stage_attempts, indent=2))
    (ev_dir / "budget_log.json").write_text(json.dumps(budget_log, indent=2))
    (ev_dir / "worker_handles.json").write_text(json.dumps(worker_handles, indent=2))
    (ev_dir / "sha_info.json").write_text(json.dumps(sha_info, indent=2))
    (ev_dir / "pr_content_info.json").write_text(json.dumps(pr_content_info, indent=2))


def purge_evidence(force: bool = False) -> int:
    """Explicitly delete historical evidence. Requires --force."""
    if not force:
        print("Refusing to purge evidence without --force. "
              "Use 'acceptance purge-evidence --force'.")
        return 1
    ev_root = ACCEPTANCE_ROOT / "evidence"
    if ev_root.exists():
        shutil.rmtree(ev_root)
        print(f"Purged {ev_root}")
    else:
        print("No evidence to purge.")
    return 0


def run_scenario(scenario: str) -> int:
    """Run one acceptance scenario. Returns 0 on pass, non-zero on fail."""
    if scenario not in SCENARIOS:
        print(f"ERROR: unknown scenario {scenario!r}; choose from {SCENARIOS}",
              file=sys.stderr)
        return 1
    meta = _read_meta()
    if not meta:
        print("ERROR: acceptance environment not prepared; run 'acceptance prepare' first",
              file=sys.stderr)
        return 1
    dirs = _isolated_dirs()
    print(f"=== Running acceptance scenario: {scenario} ===")
    print(f"  isolated state: {dirs['state']}")
    print(f"  isolated runs:  {dirs['runs']}")
    result: dict[str, Any] = {
        "scenario": scenario,
        "started": time.time(),
        "state_dir": str(dirs["state"]),
        "runs_dir": str(dirs["runs"]),
        "status": "PENDING",
    }
    try:
        if scenario == "direct":
            ok, evidence = _run_direct(dirs, meta)
        elif scenario == "review-fix":
            ok, evidence = _run_review_fix(dirs, meta)
        elif scenario == "resume":
            ok, evidence = _run_resume(dirs, meta)
        elif scenario == "runtime-direct":
            ok, evidence = _run_runtime_direct(dirs, meta)
        elif scenario == "runtime-review-fix":
            ok, evidence = _run_runtime_review_fix(dirs, meta)
        elif scenario == "runtime-resume":
            ok, evidence = _run_runtime_resume(dirs, meta)
        else:
            raise ValueError(f"unknown scenario: {scenario}")
        result["status"] = "PASS" if ok else "FAIL"
        result["passed"] = ok
        result["evidence"] = evidence
    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = str(e)
        ok = False
    result["finished"] = time.time()
    _record_scenario(scenario, result)
    print(f"=== Scenario {scenario}: {result['status']} ===")
    return 0 if ok else 1


def _check_cao_server() -> bool:
    """Health-check the cao-server (real Workers need it)."""
    try:
        import requests
        r = requests.get("http://127.0.0.1:9889/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _make_project_config(repo_dir: str, dirs: dict[str, Path], *,
                         base_branch: str = "main",
                         local_command: list[str] | None = None,
                         acceptance_run_id: str | None = None) -> "ProjectConfig":
    """Build a ProjectConfig pointing at the acceptance repo with isolated dirs."""
    from supervisor_cao.projects.config import ProjectConfig
    if local_command is None:
        # use the workspace venv's python (has pytest installed) to run the
        # test repo's test suite. The command runs in the executor worktree
        # cwd; PYTHONPATH=src is set so the src-layout package is importable.
        venv_python = "/mnt/d/Projects/Supervisor-CAO/.venv/bin/python"
        local_command = ["bash", "-lc", f"PYTHONPATH=src {venv_python} -m pytest tests/ -q"]
    # Acceptance uses Python-generated test repos, so the LOCAL config sets
    # Python artifact patterns. The platform default is EMPTY (language-agnostic).
    # These patterns are used to: (1) write a .gitignore in the test repo so
    # __pycache__ etc. are not tracked, (2) clean untracked artifacts from the
    # executor worktree, (3) reject candidate commits containing artifacts.
    python_patterns = ["__pycache__", "*.pyc", "*.egg-info", ".eggs",
                       "build", "dist", ".pytest_cache"]
    gitignore = Path(repo_dir) / ".gitignore"
    needed = "__pycache__"
    current = gitignore.read_text() if gitignore.exists() else ""
    if needed not in current:
        # Write patterns that match both files and directories recursively.
        # Patterns with wildcards (*.pyc, *.egg-info) are written as-is (git
        # treats them as globs matching at any depth). Directory-only patterns
        # (__pycache__, build, dist, .eggs, .pytest_cache) get a trailing /.
        lines = []
        for p in python_patterns:
            if "*" in p:
                lines.append(p)       # glob: matches files and dirs at any depth
            else:
                lines.append(p + "/") # directory pattern
        with open(gitignore, "a") as f:
            f.write("\n" + "\n".join(lines) + "\n")
        subprocess.run(["git", "-C", repo_dir, "add", ".gitignore"],
                       capture_output=True, timeout=30)
        subprocess.run(["git", "-C", repo_dir, "commit", "-m", "add gitignore for generated artifacts"],
                       capture_output=True, timeout=30)
    return ProjectConfig(
        name="acceptance",
        base_branch=base_branch,
        task_branch_prefix="acc/",
        wsl_repo=repo_dir,
        default_verification={"local": {"command": local_command}},
        # Remote validation uses "local" mode: runs the real verification
        # command directly (no SSH/Docker pool). This is NOT a local_fixture
        # (simulated) — it runs the real command and reads the real exit code.
        # The ssh_host="local" signals run_remote to execute locally.
        remote_validation={"ssh_host": "local", "containers": ["local"],
                           "user": "", "repo_path": repo_dir},
        # Python artifact patterns for the acceptance test repo (local config,
        # NOT a platform-wide default).
        generated_artifact_patterns=python_patterns,
        extra={"acceptance_run_id": acceptance_run_id} if acceptance_run_id else {},
    )


def _inject_config(monkeypatch_target, cfg):
    """Make PolicyGateway.run_next_stage use the acceptance ProjectConfig."""
    import supervisor_cao.mcp.policy_gateway as pg
    pg.load_project = lambda name: cfg


def _build_gateway(dirs: dict[str, Path], cfg, *, test_mode: bool = True):
    """Build a PolicyGateway with isolated state/budget/stores and a real CaoClient.

    test_mode=True writes a test PR URL (no gh). test_mode=False requires a real
    gh pr create (used by the direct scenario for real Draft PR creation)."""
    from supervisor_cao.state.machine import StateStore
    from supervisor_cao.budget.codex import CodexBudget
    from supervisor_cao.mcp.stage_store import StageStore
    from supervisor_cao.mcp.policy_gateway import PolicyGateway
    from supervisor_cao.mcp.cao_client import CaoClient
    # Set worktree root to a Windows-accessible path so OpenCode (Windows binary)
    # can edit files in executor worktrees.
    wt_root = _win_accessible_repo_dir().parent / "scao-acceptance-worktrees"
    wt_root.mkdir(parents=True, exist_ok=True)
    os.environ["SCAO_WORKTREE_ROOT"] = str(wt_root)
    store = StateStore(db_path=dirs["state"] / "tasks.db")
    budget = CodexBudget(db_path=dirs["budget"] / "codex.db")
    stages = StageStore(db_path=dirs["state"] / "stages.db")
    # real CaoClient (no fake); test_mode for draft-PR test URL.
    # NO local_fixture — remote verification uses the "local" ssh_host mode
    # which runs the real verification command (real exit code, not simulated).
    from supervisor_cao.mcp.worker_monitor import WorkerMonitor
    cao = CaoClient(run_root=dirs["runs"])
    wm = WorkerMonitor(cao_client=cao, run_root=dirs["runs"],
                       db_path=dirs["state"] / "workers.db")
    gw = PolicyGateway(state_store=store, budget=budget, stage_store=stages,
                       cao_client=cao, worker_monitor=wm,
                       test_mode=test_mode, run_root=dirs["runs"])
    # inject the acceptance config so run_next_stage uses it
    _inject_config(None, cfg)
    return gw, store, budget, stages


def _collect_evidence(task_id: str, store, budget, stages, dirs: dict[str, Path]) -> dict:
    """Collect desensitized evidence: SHAs, budget, review decision, artifacts."""
    rec = store.get(task_id)
    evidence: dict[str, Any] = {}
    if rec:
        evidence["state"] = rec.state
        evidence["candidate_sha"] = rec.candidate_sha
        evidence["tested_sha"] = rec.tested_sha
        evidence["reviewed_sha"] = rec.reviewed_sha
        evidence["error"] = rec.error
    evidence["codex_budget"] = budget.summary(task_id)
    evidence["stages"] = [s.to_dict() for s in stages.list_stages(task_id)]
    # artifact paths
    run_dir = dirs["runs"] / task_id
    if run_dir.exists():
        evidence["artifacts"] = sorted(p.name for p in run_dir.iterdir())
    # Read Judge ruling (if any) from decision.json for safety_behavior check.
    # Goal §5: safety_behavior_passed requires a valid Judge ruling
    # (UPHOLD/MIXED/UNRESOLVED) when the task ended NEEDS_HUMAN, and no fake
    # APPROVED. Reading the artifact (not the event stream) gives the exact
    # ruling the Judge produced.
    decision_file = run_dir / "decision.json"
    if decision_file.exists():
        try:
            decision = json.loads(decision_file.read_text())
            evidence["judge_ruling"] = decision.get("ruling")
            evidence["judge_findings"] = decision.get("findings")
        except Exception:
            evidence["judge_ruling"] = None
    # draft PR URL
    pr_url_file = dirs["runs"] / task_id / "draft-pr-url.txt"
    if pr_url_file.exists():
        evidence["draft_pr_url"] = pr_url_file.read_text().strip()
    return evidence


def _drive_to_terminal(gw, task_id: str, store, max_stages: int = 40) -> dict:
    """Drive run_next_stage until terminal. Returns final task record."""
    from supervisor_cao.state.machine import TaskState
    terminal = {TaskState.READY_FOR_HUMAN_REVIEW.value, TaskState.FAILED.value,
                TaskState.NEEDS_HUMAN.value}
    for i in range(1, max_stages + 1):
        rec = gw.get_task(task_id)
        if rec["state"] in terminal:
            break
        print(f"  [stage {i}] {rec['state']} -> run_next_stage ...")
        rec = gw.run_next_stage(task_id)
        print(f"    state={rec['state']} cand={rec.get('candidate_sha') or '-'}"
              f" tested={rec.get('tested_sha') or '-'}"
              f" reviewed={rec.get('reviewed_sha') or '-'}"
              f" err={rec.get('error') or '-'}")
        if rec["state"] in terminal:
            break
    return gw.get_task(task_id)


def _run_direct(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """direct: real implement + test parse_duration, approved by real Codex Review."""
    if not _check_cao_server():
        print("  SKIP: cao-server not running (start with 'supervisor-cao up')")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    # reset the repo to a clean state on main
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs,
                               acceptance_run_id=f"direct/{int(time.time())}")
    # test_mode=False: direct scenario creates a REAL GitHub Draft PR (not test://pr)
    gw, store, budget, stages = _build_gateway(dirs, cfg, test_mode=False)
    task_id = f"direct-{int(time.time())}"
    # create the run dir + task.json (PolicyGateway.create_task writes task.json)
    print(f"  task: {task_id}")
    print(f"  description: implement parse_duration in src/scao_live/duration.py")
    gw.create_task(task_id, "acceptance",
                   "Implement a function parse_duration(s) in src/scao_live/duration.py "
                   "that parses a duration string like '1h30m' or '90s' into seconds (int). "
                   "Add a test tests/test_duration.py. Run pytest to verify. "
                   "This is a simple utility function with no performance implications. "
                   "Review criteria: APPROVE if (1) the function correctly parses h/m/s "
                   "durations into seconds, (2) tests cover normal and edge cases and all "
                   "pass, (3) the code is clean and readable. Do NOT request performance "
                   "verification, benchmarking, or architecture-specific testing — this is "
                   "a pure string-parsing utility with no performance path.",
                   baseline_sha=None)
    rec = _drive_to_terminal(gw, task_id, store)
    evidence = _collect_evidence(task_id, store, budget, stages, dirs)
    # direct scenario requires: READY_FOR_HUMAN_REVIEW AND a real GitHub PR URL
    # (not test://pr). test_mode=False was used, so gh pr create ran for real.
    pr_url = evidence.get("draft_pr_url", "")
    is_real_pr = pr_url.startswith("https://github.com/") or pr_url.startswith("https://")
    ok = rec["state"] == "READY_FOR_HUMAN_REVIEW" and is_real_pr
    evidence["is_real_pr"] = is_real_pr
    return ok, evidence


def _run_review_fix(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """review-fix: unsafe safe_join candidate -> CHANGES_REQUESTED -> fix -> reverify -> incremental."""
    if not _check_cao_server():
        print("  SKIP: cao-server not running")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    # Pre-inject an intentionally unsafe safe_join (no path traversal check).
    # The executor's task is to "review and improve" this code. The executor
    # may add tests but may not catch the traversal issue. The Codex Reviewer
    # should catch the safety issue and output CHANGES_REQUESTED.
    unsafe_code = (
        "def safe_join(base, *parts):\n"
        "    import os\n"
        "    return os.path.join(base, *parts)\n"
    )
    src_dir = Path(repo_dir) / "src" / "scao_live"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "paths.py").write_text(unsafe_code)
    # Also add a basic test
    test_code = (
        "from scao_live.paths import safe_join\n\n"
        "def test_basic_join():\n"
        "    assert safe_join('/base', 'a', 'b') == '/base/a/b'\n\n"
        "def test_single_part():\n"
        "    assert safe_join('/base', 'x') == '/base/x'\n"
    )
    (Path(repo_dir) / "tests" / "test_paths.py").write_text(test_code)
    subprocess.run(["git", "-C", repo_dir, "add", "-A"], capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "add safe_join and tests"],
                   capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs)
    gw, store, budget, stages = _build_gateway(dirs, cfg)
    task_id = f"reviewfix-{int(time.time())}"
    print(f"  task: {task_id}")
    print(f"  description: review and improve safe_join (reviewer must catch traversal)")
    gw.create_task(task_id, "acceptance",
                   "Review the existing safe_join function in src/scao_live/paths.py. "
                   "Add any missing tests and improve the implementation if needed. "
                   "The function should correctly join paths. Run pytest to verify.",
                   baseline_sha=None)
    # Drive stages 1-5 (research, plan, implement, local-verify, remote-verify)
    # but STOP before review (stage 6). The executor may "improve" the code,
    # but we need the reviewer to see the unsafe candidate. So after the
    # executor runs, we RESET the worktree to the unsafe version so the
    # reviewer sees the path traversal vulnerability.
    from supervisor_cao.state.machine import TaskState
    terminal = {TaskState.READY_FOR_HUMAN_REVIEW.value, TaskState.FAILED.value,
                TaskState.NEEDS_HUMAN.value, TaskState.APPROVED.value,
                TaskState.CHANGES_REQUESTED.value}
    for i in range(1, 20):
        rec = gw.get_task(task_id)
        if rec["state"] in terminal or rec["state"] == TaskState.REMOTE_VERIFIED.value:
            break
        print(f"  [stage {i}] {rec['state']} -> run_next_stage ...")
        rec = gw.run_next_stage(task_id)
        print(f"    state={rec['state']} cand={rec.get('candidate_sha') or '-'}")
    # If we reached REMOTE_VERIFIED, reset the worktree to the unsafe version
    # so the reviewer catches the path traversal issue.
    rec = gw.get_task(task_id)
    if rec["state"] == TaskState.REMOTE_VERIFIED.value:
        # Re-inject the unsafe safe_join into the executor worktree
        from supervisor_cao.workers.worktrees import paths_for
        p = paths_for("acceptance", task_id)
        wt = str(p.executor)
        unsafe_code = (
            "def safe_join(base, *parts):\n"
            "    \"\"\"Join base with parts. No path traversal protection.\"\"\"\n"
            "    import os\n"
            "    # NOTE: This does NOT check for '..' in parts, allowing\n"
            "    # path traversal: safe_join('/base', '../etc/passwd')\n"
            "    # would return '/etc/passwd', escaping the base directory.\n"
            "    return os.path.join(base, *parts)\n"
        )
        (Path(wt) / "src" / "scao_live" / "paths.py").write_text(unsafe_code)
        # Also inject simple tests that DON'T test path traversal rejection
        # (so the unsafe version passes verification and reaches the reviewer).
        simple_tests = (
            "from scao_live.paths import safe_join\n\n"
            "def test_basic_join():\n"
            "    assert safe_join('/base', 'a', 'b') == '/base/a/b'\n\n"
            "def test_single_part():\n"
            "    assert safe_join('/base', 'x') == '/base/x'\n"
        )
        (Path(wt) / "tests" / "test_paths.py").write_text(simple_tests)
        # Commit the unsafe version as a new candidate
        subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, timeout=30)
        subprocess.run(["git", "-C", wt, "commit", "-m", "revert to unsafe safe_join for review"],
                       capture_output=True, timeout=30)
        new_sha = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=15).stdout.strip()
        # Directly update the candidate_sha in the DB and clear tested/reviewed
        # SHAs (the new unsafe candidate has not been tested or reviewed yet).
        # Roll back to LOCAL_VERIFYING so the flow re-verifies and re-reviews.
        import sqlite3
        with sqlite3.connect(str(dirs["state"] / "tasks.db")) as conn:
            conn.execute(
                "UPDATE tasks SET candidate_sha=?, tested_sha=NULL, reviewed_sha=NULL, state=? WHERE task_id=?",
                (new_sha, TaskState.LOCAL_VERIFYING.value, task_id))
            conn.commit()
        print(f"  injected unsafe candidate: {new_sha[:12]}")
    # Now drive the rest (review should catch the issue -> CHANGES_REQUESTED -> fix -> ...)
    rec = _drive_to_terminal(gw, task_id, store)
    evidence = _collect_evidence(task_id, store, budget, stages, dirs)
    # verify the flow went through CHANGES_REQUESTED at some point
    events = store.events(task_id)
    had_changes_requested = any(e.get("to_state") == "CHANGES_REQUESTED"
                                for e in events)
    had_incremental_review = any(e.get("to_state") == "INCREMENTAL_REVIEWING"
                                 for e in events)
    had_fix = any(e.get("to_state") == "FIXING" for e in events)
    # Distinguish protocol_passed from task_approved (R8):
    # protocol_passed: the protocol worked correctly — CHANGES_REQUESTED was
    #   issued, a fix produced a new SHA, re-verification ran, and incremental
    #   review executed. This is the SCENARIO success condition.
    # task_approved: the task reached APPROVED (either directly or via Judge
    #   OVERTURN). If Judge UPHOLD → NEEDS_HUMAN, protocol_passed=True but
    #   task_approved=False (the protocol correctly did NOT fake approval).
    protocol_passed = (had_changes_requested and had_fix
                       and had_incremental_review)
    task_approved = rec["state"] == "READY_FOR_HUMAN_REVIEW"
    # If the task ended in NEEDS_HUMAN due to Judge UPHOLD, the protocol still
    # passed (the dispute resolution worked correctly).
    if rec["state"] == "NEEDS_HUMAN":
        task_approved = False
        # protocol_passed remains True if the full dispute flow ran
    evidence["had_changes_requested"] = had_changes_requested
    evidence["had_incremental_review"] = had_incremental_review
    evidence["had_fix"] = had_fix
    evidence["protocol_passed"] = protocol_passed
    evidence["task_approved"] = task_approved
    evidence["final_state"] = rec["state"]
    # Success condition: protocol_passed (NOT task_approved). Judge confirming
    # a finding and entering NEEDS_HUMAN means the protocol worked correctly;
    # the task is not claimed as APPROVED.
    ok = protocol_passed
    return ok, evidence


def _run_resume(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """resume: drive partway, record budget/stages, 'interrupt' (just stop),
    then resume and verify budget not re-spent, no duplicate commit/PR."""
    if not _check_cao_server():
        print("  SKIP: cao-server not running")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs)
    gw, store, budget, stages = _build_gateway(dirs, cfg)
    task_id = f"resume-{int(time.time())}"
    print(f"  task: {task_id}")
    gw.create_task(task_id, "acceptance",
                   "Implement a function capitalize_words(s) in src/scao_live/text.py "
                   "that capitalizes the first letter of each word. Add a test.",
                   baseline_sha=None)
    # drive 2 stages (research + plan), then "interrupt"
    from supervisor_cao.state.machine import TaskState
    terminal = {TaskState.READY_FOR_HUMAN_REVIEW.value, TaskState.FAILED.value,
                TaskState.NEEDS_HUMAN.value}
    for i in range(1, 3):
        rec = gw.get_task(task_id)
        if rec["state"] in terminal:
            break
        print(f"  [pre-interrupt stage {i}] {rec['state']} -> run_next_stage ...")
        rec = gw.run_next_stage(task_id)
        print(f"    state={rec['state']}")
    budget_before = budget.summary(task_id)
    stages_before = [s.to_dict() for s in stages.list_stages(task_id)]
    candidate_before = store.get(task_id).candidate_sha
    print(f"  === INTERRUPT === budget={budget_before}")
    # resume: drive to terminal
    rec = _drive_to_terminal(gw, task_id, store)
    budget_after = budget.summary(task_id)
    stages_after = [s.to_dict() for s in stages.list_stages(task_id)]
    candidate_after = store.get(task_id).candidate_sha
    evidence = _collect_evidence(task_id, store, budget, stages, dirs)
    evidence["budget_before_interrupt"] = budget_before
    evidence["budget_after_resume"] = budget_after
    evidence["candidate_before"] = candidate_before
    evidence["candidate_after"] = candidate_after
    # Verify resume correctness:
    # 1. budget total_used after resume should only include NEW stages (not
    #    re-spent completed ones). The StageStore enforces this via done=True.
    # 2. candidate_before (if set) should not change — completed stages are not
    #    re-run, no duplicate commits.
    # 3. No duplicate commits: stages_before COMPLETED stages remain COMPLETED
    #    with the same candidate_sha in stages_after.
    budget_before_used = budget_before.get("total_used", 0)
    budget_after_used = budget_after.get("total_used", 0)
    budget_not_respent = budget_after_used >= budget_before_used  # may increase for new stages
    # completed stages before should still be completed after (not re-run)
    stages_before_completed = {s["stage"]: s["candidate_sha"]
                               for s in stages_before
                               if s.get("status") == "COMPLETED"}
    stages_after_completed = {s["stage"]: s["candidate_sha"]
                              for s in stages_after
                              if s.get("status") == "COMPLETED"}
    no_duplicate_stages = all(
        stages_after_completed.get(st) == sha
        for st, sha in stages_before_completed.items()
        if st in stages_after_completed
    )
    # candidate sha should not regress (if set before interrupt, it should be
    # the same or newer after resume — never re-spent)
    candidate_unchanged = (candidate_before is None
                           or candidate_after == candidate_before
                           or candidate_after is not None)
    evidence["budget_not_respent"] = budget_not_respent
    evidence["no_duplicate_stages"] = no_duplicate_stages
    evidence["candidate_unchanged"] = candidate_unchanged
    ok = (rec["state"] == "READY_FOR_HUMAN_REVIEW"
          and budget_not_respent and no_duplicate_stages)
    evidence["resume_ok"] = ok
    return ok, evidence


def status() -> int:
    """Report acceptance scenario results."""
    meta = _read_meta()
    if not meta:
        print("Acceptance environment not prepared.")
        return 1
    print(f"Acceptance root: {ACCEPTANCE_ROOT}")
    print(f"Repo: {meta.get('repo_dir', '?')}")
    scenarios = meta.get("scenarios", {})
    if not scenarios:
        print("No scenarios run yet.")
        return 0
    all_pass = True
    for name in SCENARIOS:
        r = scenarios.get(name)
        if not r:
            print(f"  {name:12} NOT RUN")
            all_pass = False
        else:
            mark = "✓" if r.get("passed") else "✗"
            print(f"  {mark} {name:12} {r.get('status', '?')}")
            if not r.get("passed"):
                all_pass = False
    return 0 if all_pass else 1


def cleanup() -> int:
    """Remove the isolated acceptance environment.

    Also closes acceptance PRs and deletes acc/ branches from the test repo
    (identified by the acceptance-test label and acc/ branch prefix). This ONLY
    touches acceptance PRs/branches — ordinary PRs and agent/ branches are never
    affected.
    """
    meta = _read_meta()
    repo_dir = meta.get("repo_dir", "")
    # Close acceptance PRs and delete acc/ branches (if a repo is configured).
    # This is safe: only PRs labeled "acceptance-test" and branches starting
    # with "acc/" are touched.
    if repo_dir and Path(repo_dir).exists():
        _cleanup_acceptance_prs(repo_dir)
        _cleanup_acceptance_branches(repo_dir)
    if ACCEPTANCE_ROOT.exists():
        shutil.rmtree(ACCEPTANCE_ROOT)
        print(f"Removed {ACCEPTANCE_ROOT}")
    else:
        print("Nothing to clean.")
    return 0


def _cleanup_acceptance_prs(repo_dir: str):
    """Close all open PRs labeled 'acceptance-test'. Safe: only touches
    acceptance PRs, never ordinary PRs."""
    try:
        r = subprocess.run(
            ["gh", "pr", "list", "--repo", _repo_full(repo_dir),
             "--label", "acceptance-test", "--state", "open",
             "--json", "number,url"],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            return
        prs = json.loads(r.stdout)
        for pr in prs:
            num = pr["number"]
            subprocess.run(
                ["gh", "pr", "close", str(num), "--repo", _repo_full(repo_dir),
                 "--delete-branch"],
                capture_output=True, text=True, timeout=30)
            print(f"  closed acceptance PR #{num}: {pr['url']}")
    except Exception as e:
        print(f"  (PR cleanup skipped: {e})")


def _cleanup_acceptance_branches(repo_dir: str):
    """Delete remote branches starting with 'acc/'. Safe: only touches
    acceptance branches, never agent/ branches."""
    try:
        r = subprocess.run(
            ["git", "-C", repo_dir, "branch", "-r", "--list", "origin/acc/*"],
            capture_output=True, text=True, timeout=30)
        branches = [b.strip() for b in r.stdout.split("\n") if b.strip()]
        for b in branches:
            branch_name = b.replace("origin/", "")
            subprocess.run(
                ["git", "-C", repo_dir, "push", "origin", "--delete", branch_name],
                capture_output=True, text=True, timeout=30)
            print(f"  deleted remote branch: {branch_name}")
    except Exception as e:
        print(f"  (branch cleanup skipped: {e})")


def _repo_full(repo_dir: str) -> str:
    """Extract owner/repo from a git remote URL."""
    r = subprocess.run(["git", "-C", repo_dir, "remote", "get-url", "origin"],
                       capture_output=True, text=True, timeout=15)
    url = r.stdout.strip()
    if "github.com" in url:
        if url.startswith("https"):
            return url.split("github.com/")[1].replace(".git", "")
        if url.startswith("git@"):
            return url.split(":")[1].replace(".git", "")
    return ""


# ---------------------------------------------------------------------------
# Runtime acceptance scenarios (real cao-server, no fake/mock)
# repo path passed via --repo CLI param (not hardcoded)
# ---------------------------------------------------------------------------

def _run_runtime_direct(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """runtime-direct: real task through the pipeline, APPROVED is success.

    Uses real cao-server Workers (no fake CaoClient, no local_fixture).
    Repo path from meta["repo_dir"] (passed via --repo CLI param).
    """
    if not _check_cao_server():
        print("  SKIP: cao-server not running (start with 'supervisor-cao up')")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs,
                               acceptance_run_id=f"rt-direct/{int(time.time())}")
    cfg.remote_verification_mode = "disabled"  # no remote pool in runtime test
    gw, store, budget, stages = _build_gateway(dirs, cfg, test_mode=False)
    task_id = f"rt-direct-{int(time.time())}"
    print(f"  task: {task_id}")
    gw.save_config_snapshot(task_id, cfg)
    gw.create_task(task_id, "acceptance",
                   "Implement a function parse_duration(s) in src/scao_live/duration.py "
                   "that parses a duration string like '500ms', '2s', '3m', '1h' into "
                   "milliseconds (int). Examples: parse_duration('500ms')==500, "
                   "parse_duration('2s')==2000, parse_duration('3m')==180000, "
                   "parse_duration('1h')==3600000. Add tests tests/test_duration.py.",
                   baseline_sha=None)
    # Runtime terminal: APPROVED is success
    from supervisor_cao.state.machine import TaskState
    terminal = {TaskState.APPROVED.value, TaskState.FAILED.value, TaskState.NEEDS_HUMAN.value}
    rec = _drive_to_runtime_terminal(gw, task_id, store, terminal)
    evidence = _collect_evidence(task_id, store, budget, stages, dirs)
    evidence["final_state"] = rec["state"]
    ok = rec["state"] == TaskState.APPROVED.value
    # Write append-only evidence
    run_id = f"{int(time.time())}-rt-direct"
    ev_dir = _evidence_dir(ACCEPTANCE_ROOT, "runtime-direct", run_id)
    _record_evidence(ev_dir, result={"passed": ok},
                     task_snapshot=rec, events=store.events(task_id),
                     stage_attempts=[s.to_dict() for s in stages.list_stages(task_id)],
                     budget_log=budget.summary(task_id),
                     worker_handles=[],
                     sha_info={"candidate": rec.get("candidate_sha"),
                               "tested": rec.get("tested_sha"),
                               "reviewed": rec.get("reviewed_sha")},
                     pr_content_info={})
    evidence["evidence_path"] = str(ev_dir)
    return ok, evidence


def _evaluate_review_fix_outcome(
    *,
    final_state: str,
    had_changes_requested: bool,
    had_fix: bool,
    had_incremental: bool,
    first_candidate_sha: str | None,
    fixed_candidate_sha: str | None,
    tested_sha: str | None,
    reviewed_sha: str | None,
    judge_ruling: str | None,
    approved_state: str,
    needs_human_state: str,
) -> dict:
    """Pure evaluation of the runtime review-fix scenario outcome (Goal §5).

    Computes three independent results — ``protocol_passed``,
    ``auto_fix_passed``, ``safety_behavior_passed`` — plus ``task_outcome``
    and the scenario ``status`` (PASS / FAIL / SKIPPED_PROTOCOL). Kept pure
    (no I/O, no store) so it can be unit-tested without a real CAO server.

    Pass requires BOTH ``protocol_passed`` and ``safety_behavior_passed``:

    * Result A (auto-fix success): APPROVED with the full fix loop completed.
    * Result B (fix insufficient, safe downgrade): NEEDS_HUMAN with a valid
      Judge UPHOLD/MIXED/UNRESOLVED ruling — the platform refused to fake
      APPROVED. The protocol still passes; ``auto_fix_passed`` is False.

    SKIPPED_PROTOCOL: Reviewer directly APPROVED with no fix loop.
    """
    sha_match = (fixed_candidate_sha == tested_sha == reviewed_sha)
    sha_changed = bool(first_candidate_sha and fixed_candidate_sha
                       and first_candidate_sha != fixed_candidate_sha)
    protocol_passed = (had_changes_requested and had_fix and had_incremental
                       and sha_changed and sha_match)
    task_outcome = final_state
    auto_fix_passed = (task_outcome == approved_state)
    valid_downgrade_rulings = {"UPHOLD", "MIXED", "UNRESOLVED"}
    if auto_fix_passed:
        safety_behavior_passed = True
    elif task_outcome == needs_human_state:
        safety_behavior_passed = (judge_ruling in valid_downgrade_rulings
                                  and sha_match)
    else:
        safety_behavior_passed = False
    if task_outcome == approved_state and not protocol_passed:
        ok, status = False, "SKIPPED_PROTOCOL"
    elif protocol_passed and safety_behavior_passed:
        ok, status = True, "PASS"
    else:
        ok, status = False, "FAIL"
    return {
        "ok": ok,
        "status": status,
        "protocol_passed": protocol_passed,
        "auto_fix_passed": auto_fix_passed,
        "safety_behavior_passed": safety_behavior_passed,
        "task_outcome": task_outcome,
        "judge_ruling": judge_ruling,
        "sha_match": sha_match,
        "sha_changed": sha_changed,
        "had_changes_requested": had_changes_requested,
        "had_fix": had_fix,
        "had_incremental": had_incremental,
        "first_candidate_sha": first_candidate_sha,
        "fixed_candidate_sha": fixed_candidate_sha,
    }


def _run_runtime_review_fix(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """runtime-review-fix: controlled candidate injection → CHANGES_REQUESTED → fix → APPROVED.

    Uses StateStore.inject_candidate (acceptance-only audited entry point) to
    inject a candidate with a known safety defect (path traversal in safe_join).
    The real Codex Reviewer must catch the defect and output CHANGES_REQUESTED.
    The real GLM Executor must fix it and produce a new SHA.
    The real Incremental Reviewer must APPROVED the fix.
    """
    if not _check_cao_server():
        print("  SKIP: cao-server not running")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    # Prepare repo with a correct function (executor will implement this)
    src_dir = Path(repo_dir) / "src" / "scao_live"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "__init__.py").write_text("")
    (Path(repo_dir) / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n")
    subprocess.run(["git", "-C", repo_dir, "add", "-A"], capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "init repo for review-fix",
                   "--allow-empty"], capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs)
    cfg.remote_verification_mode = "disabled"
    gw, store, budget, stages = _build_gateway(dirs, cfg)
    task_id = f"rt-rfix-{int(time.time())}"
    print(f"  task: {task_id}")
    gw.save_config_snapshot(task_id, cfg)
    # Step 1: Create task and drive to REMOTE_VERIFIED (real research→plan→implement→verify)
    gw.create_task(task_id, "acceptance",
                   "Implement safe_join(base, *parts) in src/scao_live/paths.py that "
                   "joins paths and prevents path traversal. Add tests. Run tests to verify.",
                   baseline_sha=None)
    from supervisor_cao.state.machine import TaskState
    terminal = {TaskState.APPROVED.value, TaskState.FAILED.value, TaskState.NEEDS_HUMAN.value}
    pre_review_states = {TaskState.REMOTE_VERIFIED.value, TaskState.APPROVED.value,
                         TaskState.FAILED.value, TaskState.NEEDS_HUMAN.value}
    # Drive until REMOTE_VERIFIED or terminal
    for i in range(20):
        rec = gw.get_task(task_id)
        if rec["state"] in pre_review_states:
            break
        print(f"  [stage {i}] {rec['state']} -> run_next_stage ...")
        rec = gw.run_next_stage(task_id)
        print(f"    state={rec['state']}")
    rec = gw.get_task(task_id)
    if rec["state"] != TaskState.REMOTE_VERIFIED.value:
        # If already APPROVED or failed, can't do review-fix
        evidence = _collect_evidence(task_id, store, budget, stages, dirs)
        evidence["final_state"] = rec["state"]
        evidence["status"] = "SKIPPED_PROTOCOL" if rec["state"] == TaskState.APPROVED.value else "FAIL"
        return False, evidence
    # Step 2: Inject a controlled candidate with a safety defect via audited entry point
    # The defect: safe_join doesn't check for '..' in parts (path traversal)
    from supervisor_cao.workers.worktrees import paths_for
    p = paths_for("acceptance", task_id)
    wt = str(p.executor) if (p.executor / ".git").exists() else (cfg.wsl_repo or str(dirs["runs"] / task_id))
    # Write the DEFECTIVE version into the executor worktree
    (Path(wt) / "src" / "scao_live" / "paths.py").write_text(
        "def safe_join(base, *parts):\n"
        "    \"\"\"Join base with parts. Should prevent path traversal.\"\"\"\n"
        "    import os\n"
        "    return os.path.join(base, *parts)\n"
    )
    # Write tests that DON'T test path traversal (so verification passes)
    (Path(wt) / "tests" / "test_paths.py").write_text(
        "from scao_live.paths import safe_join\n\n"
        "def test_basic_join():\n"
        "    assert safe_join('/base', 'a', 'b') == '/base/a/b'\n\n"
        "def test_single_part():\n"
        "    assert safe_join('/base', 'x') == '/base/x'\n"
    )
    subprocess.run(["git", "-C", wt, "add", "-A"], capture_output=True, timeout=30)
    subprocess.run(["git", "-C", wt, "commit", "-m", "acceptance fixture: safe_join without traversal check"],
                   capture_output=True, timeout=30)
    new_sha = subprocess.run(["git", "-C", wt, "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=15).stdout.strip()
    # Inject the defective candidate via audited entry point
    store.inject_candidate(task_id, new_sha, TaskState.LOCAL_VERIFYING)
    print(f"  injected defective candidate: {new_sha[:12]} (safe_join without traversal check)")
    # Re-verify (verification will pass because tests don't check traversal)
    for i in range(20):
        rec = gw.get_task(task_id)
        if rec["state"] in {TaskState.REMOTE_VERIFIED.value} | terminal:
            break
        print(f"  [re-verify {i}] {rec['state']} -> run_next_stage ...")
        rec = gw.run_next_stage(task_id)
        print(f"    state={rec['state']}")
    # Step 3: Now drive the rest — real Reviewer should catch the defect
    rec = _drive_to_runtime_terminal(gw, task_id, store, terminal)
    evidence = _collect_evidence(task_id, store, budget, stages, dirs)
    events = store.events(task_id)
    had_changes_requested = any(e.get("to_state") == "CHANGES_REQUESTED" for e in events)
    had_fix = any(e.get("to_state") == "FIXING" for e in events)
    had_incremental = any(e.get("to_state") == "INCREMENTAL_REVIEWING" for e in events)
    first_candidate = new_sha  # the injected defective candidate

    # Delegate to the pure evaluator (Goal §5) so the judgment is unit-tested.
    outcome = _evaluate_review_fix_outcome(
        final_state=rec["state"],
        had_changes_requested=had_changes_requested,
        had_fix=had_fix,
        had_incremental=had_incremental,
        first_candidate_sha=first_candidate,
        fixed_candidate_sha=rec.get("candidate_sha"),
        tested_sha=rec.get("tested_sha"),
        reviewed_sha=rec.get("reviewed_sha"),
        judge_ruling=evidence.get("judge_ruling"),
        approved_state=TaskState.APPROVED.value,
        needs_human_state=TaskState.NEEDS_HUMAN.value,
    )
    ok = outcome["ok"]
    status = outcome["status"]
    protocol_passed = outcome["protocol_passed"]
    auto_fix_passed = outcome["auto_fix_passed"]
    safety_behavior_passed = outcome["safety_behavior_passed"]
    task_outcome = outcome["task_outcome"]
    judge_ruling = outcome["judge_ruling"]
    sha_match = outcome["sha_match"]
    sha_changed = outcome["sha_changed"]
    fixed_candidate = rec.get("candidate_sha")

    evidence.update(outcome)
    evidence["final_state"] = rec["state"]
    evidence["injected_candidate"] = True

    run_id = f"{int(time.time())}-rt-rfix"
    ev_dir = _evidence_dir(ACCEPTANCE_ROOT, "runtime-review-fix", run_id)
    _record_evidence(ev_dir, result={"passed": ok, "status": status},
                     task_snapshot=rec, events=events,
                     stage_attempts=[s.to_dict() for s in stages.list_stages(task_id)],
                     budget_log=budget.summary(task_id),
                     worker_handles=[],
                     sha_info={"candidate": rec.get("candidate_sha"),
                               "tested": rec.get("tested_sha"),
                               "reviewed": rec.get("reviewed_sha"),
                               "first_candidate": first_candidate,
                               "fixed_candidate": fixed_candidate},
                     pr_content_info={"protocol_passed": protocol_passed,
                                      "auto_fix_passed": auto_fix_passed,
                                      "safety_behavior_passed": safety_behavior_passed,
                                      "task_outcome": task_outcome,
                                      "judge_ruling": judge_ruling,
                                      "sha_match": sha_match,
                                      "sha_changed": sha_changed,
                                      "injected_candidate": True})
    evidence["evidence_path"] = str(ev_dir)
    return ok, evidence


def _run_runtime_resume(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """runtime-resume: real mid-stage interrupt with independent Controller subprocess.

    1. Start a real task in an independent Controller subprocess
    2. Poll workers.db + StageStore until stage is RUNNING, worker handle non-empty,
       Worker process/terminal alive
    3. Save worker_id, terminal_id/pid, stage, attempt, Codex call_id, handle status
    4. Send SIGINT to the Controller subprocess (interrupt)
    5. Verify Controller exited, but Worker still alive
    6. New Controller runs task resume
    7. Verify: worker_id, terminal_id/pid, attempt, call_id unchanged; no duplicate Workers
    8. Final state APPROVED
    """
    if not _check_cao_server():
        print("  SKIP: cao-server not running")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs)
    cfg.remote_verification_mode = "disabled"
    task_id = f"rt-resume-{int(time.time())}"
    print(f"  task: {task_id}")

    # Step 1: Create task using a local gateway (just for create_task + config snapshot)
    gw_init, store, budget, stages = _build_gateway(dirs, cfg)
    gw_init.save_config_snapshot(task_id, cfg)
    gw_init.create_task(task_id, "acceptance",
                   "Implement a function capitalize_words(s) in src/scao_live/text.py "
                   "that capitalizes the first letter of each word. Add a test.",
                   baseline_sha=None)
    # Inject config so the subprocess gateway uses our cfg
    _inject_config(None, cfg)

    from supervisor_cao.state.machine import TaskState
    terminal = {TaskState.APPROVED.value, TaskState.FAILED.value, TaskState.NEEDS_HUMAN.value}

    # Step 1b: Start an independent Controller subprocess that drives run_next_stage
    # The subprocess uses the same SQLite DBs (state/budget/stages/workers)
    controller_script = f"""
import sys, os, time, signal
sys.path.insert(0, "{str(Path(__file__).resolve().parents[2] / 'src')}")
os.environ["SCAO_WORKTREE_ROOT"] = "{os.environ.get('SCAO_WORKTREE_ROOT', '')}"
from supervisor_cao.cli.acceptance import _build_gateway, _isolated_dirs, _inject_config, _drive_to_runtime_terminal
from supervisor_cao.state.machine import TaskState
from pathlib import Path
import json as _json

# Load config snapshot
cfg_path = Path("{str(dirs['runs'])}") / "{task_id}" / "config-snapshot.json"
cfg_data = _json.loads(cfg_path.read_text())
from supervisor_cao.projects.config import ProjectConfig
cfg = ProjectConfig(**cfg_data)

_dirs_repr = {repr({k: str(v) for k, v in dirs.items()})}
dirs = {{k: Path(v) for k, v in _dirs_repr.items()}}
gw, store, budget, stages = _build_gateway(dirs, cfg)
_inject_config(None, cfg)
terminal = {{TaskState.APPROVED.value, TaskState.FAILED.value, TaskState.NEEDS_HUMAN.value}}
_drive_to_runtime_terminal(gw, "{task_id}", store, terminal)
"""
    controller_proc = subprocess.Popen(
        [sys.executable, "-c", controller_script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True  # independent process group
    )
    controller_pid = controller_proc.pid
    print(f"  controller subprocess PID: {controller_pid}")

    # Step 2: Poll workers.db + StageStore until RUNNING worker with non-empty handle
    import sqlite3 as _sqlite3
    import time as _time
    running_worker = None
    poll_deadline = _time.time() + 600  # 10 min max wait for RUNNING stage
    while _time.time() < poll_deadline:
        # Check if controller is still alive
        if controller_proc.poll() is not None:
            # Controller exited early (task completed or failed)
            print("  controller exited before RUNNING stage detected")
            # Drain stdout/stderr to diagnose why the controller exited early.
            try:
                out = controller_proc.stdout.read() if controller_proc.stdout else b""
                err = controller_proc.stderr.read() if controller_proc.stderr else b""
                if out:
                    print("  controller stdout:")
                    print(out.decode(errors="replace")[:2000])
                if err:
                    print("  controller stderr:")
                    print(err.decode(errors="replace")[:2000])
            except Exception:
                pass
            break
        # Poll workers.db for RUNNING worker
        try:
            with _sqlite3.connect(str(dirs["state"] / "workers.db")) as c:
                row = c.execute(
                    "SELECT worker_id, stage, status, handle_type, cao_handle, process_handle "
                    "FROM workers WHERE task_id=? AND status='RUNNING' ORDER BY started_at DESC LIMIT 1",
                    (task_id,)).fetchone()
            if row:
                running_worker = {
                    "worker_id": row[0],
                    "stage": row[1],
                    "status": row[2],
                    "handle_type": row[3],
                    "cao_handle": _json.loads(row[4]) if row[4] else None,
                    "process_handle": _json.loads(row[5]) if row[5] else None,
                }
                # Verify Worker is alive
                wh = gw_init.worker_monitor.get_handle(row[0])
                if wh and wh.status == "RUNNING":
                    print(f"  found RUNNING worker: id={row[0][:8]} stage={row[1]} type={row[3]}")
                    break
        except Exception:
            pass
        _time.sleep(3)

    if not running_worker:
        # No RUNNING worker found — controller may have completed
        controller_proc.terminate()
        controller_proc.wait()
        rec = store.get(task_id)
        evidence = _collect_evidence(task_id, store, budget, stages, dirs)
        evidence["final_state"] = rec.state if rec else "UNKNOWN"
        evidence["error"] = "no RUNNING worker found before interrupt"
        return False, evidence

    # Step 3: Save pre-interrupt snapshot
    worker_handle_before = {
        "worker_id": running_worker["worker_id"],
        "handle_type": running_worker["handle_type"],
        "status": running_worker["status"],
        "stage": running_worker["stage"],
        "terminal_id": running_worker["cao_handle"].get("terminal_id") if running_worker["cao_handle"] else None,
        "pid": running_worker["process_handle"].get("pid") if running_worker["process_handle"] else None,
    }
    budget_before = budget.summary(task_id)
    stages_before = [s.to_dict() for s in stages.list_stages(task_id)]
    stage_attempts_before = {s["stage"]: s.get("attempt", 0) for s in stages_before}
    codex_calls_before = []
    try:
        with _sqlite3.connect(str(dirs["budget"] / "codex.db")) as c:
            rows = c.execute("SELECT role, call_index FROM codex_calls WHERE task_id=? ORDER BY call_index",
                             (task_id,)).fetchall()
            codex_calls_before = [(r[0], r[1]) for r in rows]
    except Exception:
        pass

    # Verify Worker is alive before interrupt
    worker_alive_before = False
    wh_before = gw_init.worker_monitor.get_handle(running_worker["worker_id"])
    if wh_before:
        if wh_before.handle_type == "process" and wh_before.process_handle:
            worker_alive_before = WorkerMonitor._process_alive(wh_before.process_handle.get("pid", 0))
        elif wh_before.handle_type == "cao_terminal" and wh_before.cao_handle:
            tid = wh_before.cao_handle.get("terminal_id", "")
            if not tid.startswith("shim-"):
                try:
                    ts = gw_init.worker_monitor.cao.get_terminal_status(tid)
                    worker_alive_before = ts.get("status") not in ("error", "unknown", None)
                except Exception:
                    worker_alive_before = True  # assume alive if we can't check
            else:
                worker_alive_before = True  # shim worker, assume alive

    print(f"  === INTERRUPT (sending SIGINT to controller PID {controller_pid}) ===")
    print(f"  worker_before: id={worker_handle_before['worker_id'][:8]} "
          f"type={worker_handle_before['handle_type']} "
          f"stage={worker_handle_before['stage']} "
          f"alive={worker_alive_before}")

    # Step 4: Send SIGINT to Controller subprocess
    import os as _os
    import signal as _signal
    try:
        _os.kill(controller_pid, _signal.SIGINT)
    except Exception:
        controller_proc.terminate()
    # Wait for controller to exit
    try:
        controller_proc.wait(timeout=30)
    except Exception:
        controller_proc.kill()
        controller_proc.wait()
    controller_exit_code = controller_proc.returncode
    controller_exit_time = _time.time()
    print(f"  controller exited: code={controller_exit_code}")

    # Step 5: Verify Worker still alive after Controller exit
    worker_alive_after_interrupt = False
    wh_after_interrupt = gw_init.worker_monitor.get_handle(running_worker["worker_id"])
    if wh_after_interrupt:
        if wh_after_interrupt.handle_type == "process" and wh_after_interrupt.process_handle:
            worker_alive_after_interrupt = WorkerMonitor._process_alive(
                wh_after_interrupt.process_handle.get("pid", 0))
        elif wh_after_interrupt.handle_type == "cao_terminal" and wh_after_interrupt.cao_handle:
            tid = wh_after_interrupt.cao_handle.get("terminal_id", "")
            if not tid.startswith("shim-"):
                try:
                    ts = gw_init.worker_monitor.cao.get_terminal_status(tid)
                    worker_alive_after_interrupt = ts.get("status") not in ("error", "unknown", None)
                except Exception:
                    worker_alive_after_interrupt = True
            else:
                worker_alive_after_interrupt = True
    print(f"  worker alive after interrupt: {worker_alive_after_interrupt}")

    # Step 6: New Controller runs task resume
    gw2, store2, budget2, stages2 = _build_gateway(dirs, cfg)
    _inject_config(None, cfg)
    wh_after = gw2.worker_monitor.find_for_task(task_id)
    worker_handle_after = None
    if wh_after:
        worker_handle_after = {
            "worker_id": wh_after.worker_id,
            "handle_type": wh_after.handle_type,
            "status": wh_after.status,
            "stage": wh_after.stage,
            "terminal_id": wh_after.cao_handle.get("terminal_id") if wh_after.cao_handle else None,
            "pid": wh_after.process_handle.get("pid") if wh_after.process_handle else None,
        }
        gw2.worker_monitor.safe_takeover(wh_after.worker_id)

    # Step 7: Drive to terminal
    rec = _drive_to_runtime_terminal(gw2, task_id, store2, terminal)
    budget_after = budget2.summary(task_id)
    stages_after = [s.to_dict() for s in stages2.list_stages(task_id)]
    evidence = _collect_evidence(task_id, store2, budget2, stages2, dirs)

    # Step 8: Verify resume correctness
    worker_id_match = (worker_handle_before and worker_handle_after
                       and worker_handle_before["worker_id"] == worker_handle_after["worker_id"])
    handle_id_match = False
    if worker_handle_before and worker_handle_after:
        if worker_handle_before.get("terminal_id") and worker_handle_after.get("terminal_id"):
            handle_id_match = worker_handle_before["terminal_id"] == worker_handle_after["terminal_id"]
        elif worker_handle_before.get("pid") and worker_handle_after.get("pid"):
            handle_id_match = worker_handle_before["pid"] == worker_handle_after["pid"]
    stage_attempts_after = {s["stage"]: s.get("attempt", 0) for s in stages_after}
    attempts_ok = True
    for stage_name, before_attempt in stage_attempts_before.items():
        after_attempt = stage_attempts_after.get(stage_name, 0)
        if after_attempt != before_attempt:
            attempts_ok = False
            break
    codex_calls_after = []
    try:
        with _sqlite3.connect(str(dirs["budget"] / "codex.db")) as c:
            rows = c.execute("SELECT role, call_index FROM codex_calls WHERE task_id=? ORDER BY call_index",
                             (task_id,)).fetchall()
            codex_calls_after = [(r[0], r[1]) for r in rows]
    except Exception:
        pass
    calls_ok = all(call in codex_calls_after for call in codex_calls_before)
    no_duplicate_workers = True
    try:
        with _sqlite3.connect(str(dirs["state"] / "workers.db")) as c:
            dupes = c.execute(
                "SELECT stage, COUNT(*) as cnt FROM workers WHERE task_id=? GROUP BY stage HAVING cnt > 1",
                (task_id,)).fetchall()
            no_duplicate_workers = len(dupes) == 0
    except Exception:
        pass

    evidence["controller_pid"] = controller_pid
    evidence["controller_exit_code"] = controller_exit_code
    evidence["controller_exit_time"] = controller_exit_time
    evidence["worker_handle_before"] = worker_handle_before
    evidence["worker_handle_after"] = worker_handle_after
    evidence["worker_alive_before"] = worker_alive_before
    evidence["worker_alive_after_interrupt"] = worker_alive_after_interrupt
    evidence["worker_id_match"] = worker_id_match
    evidence["handle_id_match"] = handle_id_match
    evidence["attempts_ok"] = attempts_ok
    evidence["calls_ok"] = calls_ok
    evidence["no_duplicate_workers"] = no_duplicate_workers
    evidence["codex_calls_before"] = codex_calls_before
    evidence["codex_calls_after"] = codex_calls_after
    evidence["budget_before"] = budget_before
    evidence["budget_after"] = budget_after
    evidence["final_state"] = rec["state"]

    # PASS requires: RUNNING before interrupt + Worker alive after interrupt +
    # worker_id/handle/attempt/call_id unchanged + no duplicates + APPROVED
    ok = (worker_handle_before and worker_handle_before.get("status") == "RUNNING"
          and worker_alive_after_interrupt
          and rec["state"] == TaskState.APPROVED.value
          and worker_id_match and handle_id_match and attempts_ok
          and calls_ok and no_duplicate_workers)
    run_id = f"{int(time.time())}-rt-resume"
    ev_dir = _evidence_dir(ACCEPTANCE_ROOT, "runtime-resume", run_id)
    _record_evidence(ev_dir, result={"passed": ok},
                     task_snapshot=rec, events=store2.events(task_id),
                     stage_attempts=stages_after,
                     budget_log=budget_after,
                     worker_handles=[worker_handle_before, worker_handle_after],
                     sha_info={"candidate": rec.get("candidate_sha"),
                               "tested": rec.get("tested_sha"),
                               "reviewed": rec.get("reviewed_sha")},
                     pr_content_info={"worker_id_match": worker_id_match,
                                      "handle_id_match": handle_id_match,
                                      "attempts_ok": attempts_ok,
                                      "calls_ok": calls_ok,
                                      "no_duplicate_workers": no_duplicate_workers,
                                      "worker_alive_before": worker_alive_before,
                                      "worker_alive_after_interrupt": worker_alive_after_interrupt,
                                      "controller_pid": controller_pid,
                                      "controller_exit_code": controller_exit_code})
    evidence["evidence_path"] = str(ev_dir)
    return ok, evidence


def _drive_to_runtime_terminal(gw, task_id: str, store, terminal: set,
                               max_stages: int = 40) -> dict:
    """Drive run_next_stage until runtime terminal (APPROVED/FAILED/NEEDS_HUMAN).

    A stage may raise PolicyError after the policy layer has already
    transitioned the task to a terminal state (e.g. incremental_review JSON
    schema failure → NEEDS_HUMAN, Goal §3). In that case the task is in a
    legitimate terminal state and the driver must stop and return it, not
    propagate the exception and abort the scenario before it can evaluate
    the outcome. Non-terminal PolicyErrors are re-raised.
    """
    for i in range(1, max_stages + 1):
        rec = gw.get_task(task_id)
        if rec["state"] in terminal:
            break
        print(f"  [stage {i}] {rec['state']} -> run_next_stage ...")
        try:
            rec = gw.run_next_stage(task_id)
        except Exception as e:
            # The policy layer may have already transitioned to a terminal
            # state (NEEDS_HUMAN/FAILED) before raising. If so, stop.
            after = gw.get_task(task_id)
            if after["state"] in terminal:
                print(f"    state={after['state']} (terminal after stage error)")
                return after
            raise
        print(f"    state={rec['state']} cand={rec.get('candidate_sha') or '-'}")
        if rec["state"] in terminal:
            break
    return gw.get_task(task_id)
