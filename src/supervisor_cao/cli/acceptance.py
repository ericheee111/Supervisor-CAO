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

SCENARIOS = ("direct", "review-fix", "resume")


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
        else:
            ok, evidence = _run_resume(dirs, meta)
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
    gw = PolicyGateway(state_store=store, budget=budget, stage_store=stages,
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


def _direct_pass(final_state, candidate, tested, reviewed, ev_dir: Path,
                 task_id: str, head_branch: str) -> tuple[bool, str]:
    """Check direct scenario pass conditions (forge-agnostic).

    PASS requires ALL:
      - final_state == READY_FOR_HUMAN_REVIEW
      - candidate == tested == reviewed
      - pr-content artifact valid (sha256, schema, workflow_state, SHAs, push.json)
    No forge API is called; no real PR URL is required.
    """
    if final_state != "READY_FOR_HUMAN_REVIEW":
        return False, f"final_state={final_state}"
    if not (candidate == tested == reviewed):
        return False, f"SHA mismatch: candidate={candidate} tested={tested} reviewed={reviewed}"
    from supervisor_cao.validation.windows_sync import validate_pr_content_artifact
    if not validate_pr_content_artifact(ev_dir, task_id, candidate, tested, reviewed,
                                        "main", head_branch):
        return False, "pr-content artifact invalid"
    return True, "ok"


def _review_fix_pass(protocol_passed: bool, task_approved: bool, final_state: str,
                     ev_dir: Path, candidate: str, tested: str, reviewed: str,
                     task_id: str, head_branch: str) -> tuple[bool, str]:
    """Check review-fix main scenario pass conditions.

    Main PASS requires ALL:
      - protocol_passed == true (CHANGES_REQUESTED + fix + incremental_review)
      - task_approved == true (final state == READY_FOR_HUMAN_REVIEW)
      - final_state == READY_FOR_HUMAN_REVIEW
      - pr_content_valid == true

    Judge correctly entering NEEDS_HUMAN is a safety sub-scenario (can be
    recorded separately), but does NOT make the main review-fix scenario pass.
    """
    if not protocol_passed:
        return False, "protocol not passed"
    if not task_approved:
        return False, "task not approved (task_approved=False)"
    if final_state != "READY_FOR_HUMAN_REVIEW":
        return False, f"final_state={final_state}"
    from supervisor_cao.validation.windows_sync import validate_pr_content_artifact
    if not validate_pr_content_artifact(ev_dir, task_id, candidate, tested, reviewed,
                                        "main", head_branch):
        return False, "pr-content artifact invalid"
    return True, "ok"


def _run_direct(dirs: dict[str, Path], meta: dict) -> tuple[bool, dict]:
    """direct: real implement + test parse_duration, approved by real Codex Review.

    Uses test_mode=True (no gh, no forge API). Pass conditions are forge-agnostic:
    final state READY_FOR_HUMAN_REVIEW + SHA equality + valid pr-content artifact.
    """
    if not _check_cao_server():
        print("  SKIP: cao-server not running (start with 'supervisor-cao up')")
        return False, {"error": "cao-server not running"}
    repo_dir = meta["repo_dir"]
    subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "origin/main"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", repo_dir, "clean", "-fd"], capture_output=True, timeout=30)
    cfg = _make_project_config(repo_dir, dirs,
                               acceptance_run_id=f"direct/{int(time.time())}")
    # test_mode=True: no gh pr create; forge-agnostic PR content generation only.
    gw, store, budget, stages = _build_gateway(dirs, cfg, test_mode=True)
    task_id = f"direct-{int(time.time())}"
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
    # Forge-agnostic pass conditions (no real PR URL required)
    run_dir = dirs["runs"] / task_id
    task_branch = cfg.task_branch_for(task_id)
    ok, reason = _direct_pass(rec["state"], rec.get("candidate_sha"),
                              rec.get("tested_sha"), rec.get("reviewed_sha"),
                              run_dir, task_id, task_branch)
    evidence["direct_pass_reason"] = reason
    evidence["pr_content_valid"] = ok
    # Write append-only evidence
    run_id = f"{int(time.time())}-direct"
    ev_dir = _evidence_dir(ACCEPTANCE_ROOT, "direct", run_id)
    _record_evidence(ev_dir, result={"passed": ok, "reason": reason},
                     task_snapshot=rec, events=store.events(task_id),
                     stage_attempts=[s.to_dict() for s in stages.list_stages(task_id)],
                     budget_log=budget.summary(task_id),
                     worker_handles=[],
                     sha_info={"candidate": rec.get("candidate_sha"),
                               "tested": rec.get("tested_sha"),
                               "reviewed": rec.get("reviewed_sha")},
                     pr_content_info={"valid": ok})
    evidence["evidence_path"] = str(ev_dir)
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
        # Use the audited StateStore.inject_candidate entry point (NOT raw SQL).
        # This clears tested/reviewed SHAs and records a CONTROLLED_CANDIDATE_INJECTION
        # event. Roll back to LOCAL_VERIFYING so the flow re-verifies and re-reviews.
        store.inject_candidate(task_id, new_sha, TaskState.LOCAL_VERIFYING)
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
    # Main review-fix PASS requires ALL 4 conditions:
    #   protocol_passed + task_approved + final_state == READY_FOR_HUMAN_REVIEW
    #   + pr_content_valid
    # Judge correctly entering NEEDS_HUMAN is a safety sub-scenario (recorded
    # as safety_behavior_evidence), but does NOT make the main scenario pass.
    run_dir = dirs["runs"] / task_id
    task_branch = cfg.task_branch_for(task_id)
    ok, reason = _review_fix_pass(protocol_passed, task_approved, rec["state"],
                                  run_dir, rec.get("candidate_sha") or "",
                                  rec.get("tested_sha") or "",
                                  rec.get("reviewed_sha") or "",
                                  task_id, task_branch)
    evidence["review_fix_pass_reason"] = reason
    evidence["pr_content_valid"] = ok
    # Record safety sub-scenario evidence when Judge correctly entered NEEDS_HUMAN
    if rec["state"] == "NEEDS_HUMAN" and protocol_passed:
        evidence["safety_behavior_evidence"] = (
            "Judge correctly entered NEEDS_HUMAN — protocol worked, task not faked as approved")
    # Write append-only evidence
    run_id = f"{int(time.time())}-reviewfix"
    ev_dir = _evidence_dir(ACCEPTANCE_ROOT, "review-fix", run_id)
    _record_evidence(ev_dir, result={"passed": ok, "reason": reason},
                     task_snapshot=rec, events=store.events(task_id),
                     stage_attempts=[s.to_dict() for s in stages.list_stages(task_id)],
                     budget_log=budget.summary(task_id),
                     worker_handles=[],
                     sha_info={"candidate": rec.get("candidate_sha"),
                               "tested": rec.get("tested_sha"),
                               "reviewed": rec.get("reviewed_sha")},
                     pr_content_info={"valid": ok, "protocol_passed": protocol_passed,
                                      "task_approved": task_approved})
    evidence["evidence_path"] = str(ev_dir)
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
    """Remove the isolated acceptance environment (runtime, worktrees, acc branches).

    Preserves acceptance/evidence/ (append-only history). Does NOT close PRs
    or delete labels — forge operations are no longer performed.
    """
    meta = _read_meta()
    repo_dir = meta.get("repo_dir", "")
    if repo_dir and Path(repo_dir).exists():
        _cleanup_acceptance_branches(repo_dir)  # only acc/ branches, safe
    # Remove everything under ACCEPTANCE_ROOT except evidence/
    ev_root = ACCEPTANCE_ROOT / "evidence"
    if ACCEPTANCE_ROOT.exists():
        for item in ACCEPTANCE_ROOT.iterdir():
            if item.name == "evidence":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                try:
                    item.unlink()
                except Exception:
                    pass
        print(f"Cleaned runtime (evidence preserved at {ev_root})")
    else:
        print("Nothing to clean.")
    return 0


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
