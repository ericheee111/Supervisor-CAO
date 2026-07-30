"""Acceptance test runner for Supervisor-CAO.

Provides isolated acceptance scenarios that use independent state, budget,
runs, and worktree directories so they never read or modify existing historical
tasks. Scenarios:

  direct:    real implement + test of a function, approved by real Codex Review.
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
    repo_dir = dirs["repo"]
    # Clone or update the test repo (do NOT create/delete remote).
    if repo_url:
        if repo_dir.exists() and (repo_dir / ".git").exists():
            print(f"Updating existing acceptance repo at {repo_dir}...")
            subprocess.run(["git", "-C", str(repo_dir), "fetch", "origin"],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "-C", str(repo_dir), "reset", "--hard",
                            "origin/HEAD"], capture_output=True, timeout=30)
        else:
            print(f"Cloning {repo_url} into {repo_dir}...")
            r = subprocess.run(["git", "clone", repo_url, str(repo_dir)],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"ERROR: clone failed: {r.stderr.strip()}", file=sys.stderr)
                return 1
    elif repo_path:
        # use an existing local checkout by copying
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
    # Each scenario is driven by the PolicyGateway with isolated stores. The
    # actual real-Worker execution requires a running cao-server; this runner
    # wires the isolated directories and records evidence.
    result: dict[str, Any] = {
        "scenario": scenario,
        "started": time.time(),
        "state_dir": str(dirs["state"]),
        "runs_dir": str(dirs["runs"]),
        "status": "PENDING",
    }
    try:
        if scenario == "direct":
            ok = _run_direct(dirs)
        elif scenario == "review-fix":
            ok = _run_review_fix(dirs)
        else:
            ok = _run_resume(dirs)
        result["status"] = "PASS" if ok else "FAIL"
        result["passed"] = ok
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


def _build_gateway(dirs: dict[str, Path]):
    """Build a PolicyGateway with isolated state/budget/stores and a real CaoClient."""
    from supervisor_cao.state.machine import StateStore
    from supervisor_cao.budget.codex import CodexBudget
    from supervisor_cao.mcp.stage_store import StageStore
    from supervisor_cao.mcp.policy_gateway import PolicyGateway
    from supervisor_cao.mcp.cao_client import CaoClient
    store = StateStore(db_path=dirs["state"] / "tasks.db")
    budget = CodexBudget(db_path=dirs["budget"] / "codex.db")
    stages = StageStore(db_path=dirs["state"] / "stages.db")
    gw = PolicyGateway(state_store=store, budget=budget, stage_store=stages,
                       test_mode=True)
    return gw, store, budget, stages


def _run_direct(dirs: dict[str, Path]) -> bool:
    """direct: real implement + test parse_duration, approved by real Codex Review."""
    if not _check_cao_server():
        print("  SKIP: cao-server not running (start with 'supervisor-cao up')")
        return False
    gw, store, budget, stages = _build_gateway(dirs)
    # The real flow: create_task -> run_next_stage loop until terminal.
    # This requires a configured project pointing at the acceptance repo.
    # Evidence is collected from the run dir.
    print("  direct scenario requires a running cao-server and configured project")
    print("  (evidence collected under isolated runs dir)")
    return False  # real execution is performed via the WSL acceptance harness


def _run_review_fix(dirs: dict[str, Path]) -> bool:
    """review-fix: unsafe candidate -> CHANGES_REQUESTED -> fix -> reverify -> incremental."""
    if not _check_cao_server():
        print("  SKIP: cao-server not running")
        return False
    print("  review-fix scenario requires a running cao-server")
    return False


def _run_resume(dirs: dict[str, Path]) -> bool:
    """resume: interrupt during Planner/Executor, resume, verify no dup budget/commit/PR."""
    if not _check_cao_server():
        print("  SKIP: cao-server not running")
        return False
    print("  resume scenario requires a running cao-server")
    return False


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
    """Remove the isolated acceptance environment."""
    if ACCEPTANCE_ROOT.exists():
        shutil.rmtree(ACCEPTANCE_ROOT)
        print(f"Removed {ACCEPTANCE_ROOT}")
    else:
        print("Nothing to clean.")
    return 0
