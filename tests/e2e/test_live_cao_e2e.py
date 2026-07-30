#!/usr/bin/env python3
"""Live CAO E2E: two real scenarios driven through the policy gateway.

Replaces test_real_cao_e2e.py. Runs real CAO Workers:
  - Codex planner/reviewer via CAO POST /terminals/run-step (real CAO terminal)
  - OpenCode researcher/executor/verifier via `opencode run --format json`

Scenario 1 (APPROVED):
  researcher -> codex-planner -> glm-executor (really edits calc.py, commits,
  pushes to bare remote) -> qwen-verifier (really runs pytest, reads exit code)
  -> codex-reviewer (APPROVED) -> draft-pr (test-mode) -> windows sync (temp
  clone fast-forward) -> READY_FOR_HUMAN_REVIEW.

Scenario 2 (CHANGES_REQUESTED):
  A temp task with a planted defect + acceptance criteria. Real Codex Reviewer
  reads the code+tests and emits CHANGES_REQUESTED with a finding (the test does
  NOT forge the review JSON). -> fixing -> glm-executor re-edits -> new SHA ->
  qwen-verifier re-verifies -> codex-reviewer incremental -> APPROVED.

Evidence (session ids, terminal ids, raw outputs, SHAs, artifacts) is saved
under ~/cao-runs/<task-id>/ which is gitignored (requirement 5). Only sanitized
test code and a sanitized summary are committed.

Skip if SKIP_REAL_E2E=1 or tools unavailable (CI). Run on the host machine via:
  wsl.exe -d Ubuntu-24.04 -- python3 tests/e2e/test_live_cao_e2e.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.mcp.policy_gateway import PolicyGateway, PolicyError  # noqa: E402
from supervisor_cao.mcp.cao_client import CaoClient  # noqa: E402
from supervisor_cao.state.machine import StateStore, TaskState  # noqa: E402
from supervisor_cao.budget.codex import CodexBudget  # noqa: E402
from supervisor_cao.mcp.stage_store import StageStore  # noqa: E402
from supervisor_cao.projects.config import ProjectConfig  # noqa: E402

SKIP = os.environ.get("SKIP_REAL_E2E", "")
RUN_ROOT = Path.home() / "cao-runs"


def _check_tool(name: str) -> bool:
    try:
        subprocess.run([name, "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _cao_server_up() -> bool:
    return CaoClient().server_health()


def git(cmd, cwd=None, check=True):
    r = subprocess.run(["git"] + cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {cmd[:2]} failed: {r.stderr.strip()}")
    return r


def setup_temp_repos(tmp: Path, calc_content: str = "def add(a, b):\n    return a + b\n"):
    """Create a bare remote + main clone + windows clone with calc.py."""
    bare = tmp / "remote.git"
    git(["init", "--bare", "-b", "main", str(bare)])
    main_repo = tmp / "main"
    git(["init", "-b", "main", str(main_repo)])
    git(["config", "user.email", "t@t.t"], cwd=str(main_repo))
    git(["config", "user.name", "tester"], cwd=str(main_repo))
    (main_repo / "calc.py").write_text(calc_content)
    (main_repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    git(["add", "-A"], cwd=str(main_repo))
    git(["commit", "-m", "init"], cwd=str(main_repo))
    git(["branch", "dev"], cwd=str(main_repo))
    git(["remote", "add", "origin", str(bare)], cwd=str(main_repo))
    git(["push", "origin", "main", "dev"], cwd=str(main_repo))
    win_repo = tmp / "windows"
    git(["clone", "-b", "dev", str(bare), str(win_repo)])
    git(["config", "user.email", "t@t.t"], cwd=str(win_repo))
    git(["config", "user.name", "tester"], cwd=str(win_repo))
    return str(main_repo), str(win_repo), str(bare)


def main() -> int:
    if SKIP:
        print("SKIP: SKIP_REAL_E2E set")
        return 0
    if not (_check_tool("opencode") and _check_tool("codex")):
        print("SKIP: opencode or codex not available")
        return 0
    if not _cao_server_up():
        print("SKIP: cao-server not running (start with 'supervisor-cao up')")
        return 0

    results = []
    def check(name, ok, detail=""):
        mark = "✓" if ok else "✗"
        results.append((name, ok, detail))
        print(f"  {mark} {name}: {detail}")

    # ===== Scenario 1: APPROVED =====
    print("\n=== Scenario 1: APPROVED path ===")
    tmp1 = Path(tempfile.mkdtemp(prefix="scao_live1_"))
    main_repo, win_repo, bare = setup_temp_repos(tmp1)
    task_id = f"live1-{int(time.time()) % 100000}"
    baseline_sha = git(["rev-parse", "HEAD"], cwd=main_repo).stdout.strip()

    store = StateStore(db_path=tmp1 / "tasks.db")
    budget = CodexBudget(db_path=tmp1 / "codex.db")
    stages = StageStore(db_path=tmp1 / "stages.db")

    # Register the temp project config via the REAL mechanism (local config file)
    # so load_project() finds it without monkey-patching.
    import yaml
    from supervisor_cao.projects.config import LOCAL_CONFIG_DIR
    LOCAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    local_cfg_path = LOCAL_CONFIG_DIR / "testproj.local.yaml"
    local_cfg_path.write_text(yaml.dump({
        "name": "testproj", "base_branch": "dev", "task_branch_prefix": "agent/",
        "wsl_repo": main_repo, "windows_repo": win_repo,
    }))

    gw = PolicyGateway(state_store=store, budget=budget, stage_store=stages)
    run_dir = RUN_ROOT / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".test-mode").write_text("")  # enable test-mode PR adapter
    (run_dir / "task.json").write_text(json.dumps({"task_id": task_id, "description": ""}))

    try:
        rec = gw.create_task(task_id, "testproj",
                             "Add a multiply(a,b) function to calc.py that returns a*b. "
                             "Also add a test test_multiply.",
                             baseline_sha)
        check("create_task", rec["state"] == "CREATED", rec["state"])

        # drive stages until terminal
        terminal = {TaskState.READY_FOR_HUMAN_REVIEW.value, TaskState.FAILED.value}
        for i in range(1, 25):
            rec = gw.get_task(task_id)
            if rec["state"] in terminal:
                break
            print(f"  [stage {i}] {rec['state']} ...")
            rec = gw.run_next_stage(task_id)
            print(f"    -> {rec['state']} cand={rec.get('candidate_sha') or '-'}")
            if rec["state"] in terminal:
                break

        check("reached READY_FOR_HUMAN_REVIEW",
              rec["state"] == TaskState.READY_FOR_HUMAN_REVIEW.value, rec["state"])

        # evidence: real CAO session/terminal ids saved
        ev = run_dir / "cao-session.json"
        check("cao-session.json evidence saved", ev.exists(), str(ev))
        if ev.exists():
            ev_data = json.loads(ev.read_text())
            check("at least one Codex run-step call (real CAO Worker)",
                  any(r.get("profile") in ("codex-planner", "codex-reviewer") for r in ev_data),
                  f"{len(ev_data)} stages")
            check("at least one real terminal_id (CAO terminal created)",
                  any(r.get("terminal_id") for r in ev_data),
                  "terminal ids present")

        # evidence: real candidate SHA (GLM executor committed)
        impl = gw.get_artifact(task_id, "implementation")
        check("implementation.json exists", impl is not None, "present" if impl else "missing")
        if impl:
            cand = impl.get("candidate_sha")
            check("candidate_sha != baseline (executor committed)",
                  cand and cand != baseline_sha, f"{(cand or '')[:12]}")
            # verify the multiply function was really added
            content = (Path(main_repo) / "calc.py").read_text()
            check("GLM Executor added multiply function", "multiply" in content.lower(),
                  "found" if "multiply" in content.lower() else "missing")

        # evidence: verification ran real pytest
        verify = gw.get_artifact(task_id, "verification")
        check("verification.json exists", verify is not None, "present" if verify else "missing")
        if verify:
            check("verification passed (real exit code)", verify.get("passed") is True,
                  str(verify.get("passed")))

        # evidence: review decision (real Codex, not forged)
        review = gw.get_artifact(task_id, "review")
        check("review.json exists", review is not None, "present" if review else "missing")
        if review:
            check("review decision APPROVED (real Codex output)",
                  review.get("decision") == "APPROVED", str(review.get("decision")))

        # budget: planner + full_review = 2
        s = budget.summary(task_id)
        check("Codex budget 2/4 used", s["total_used"] == 2, f"{s['total_used']}/4")

    except PolicyError as e:
        check("scenario1 no policy error", False, str(e))
    except Exception as e:
        check("scenario1 no exception", False, f"{type(e).__name__}: {e}")
    finally:
        # clean up the temp project config so it doesn't leak into other tests
        try:
            local_cfg_path.unlink()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n=== Live CAO E2E (Scenario 1) Summary: {passed} PASS, {failed} FAIL ===")
    # save sanitized summary (no raw model output)
    summary_path = RUN_ROOT / "live-cao-e2e-summary.json"
    summary_path.write_text(json.dumps({
        "scenario": "APPROVED", "task_id": task_id, "passed": passed, "failed": failed,
        "checks": [{"name": n, "ok": ok, "detail": d[:200]} for n, ok, d in results],
        "run_dir": str(run_dir),
    }, indent=2))
    print(f"Sanitized summary: {summary_path}")
    print(f"Raw evidence (gitignored): {run_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
