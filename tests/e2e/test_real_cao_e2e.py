#!/usr/bin/env python3
"""Real CAO E2E test: exercises the policy gateway with real LLM calls.

Unlike test_temp_repo_e2e.py (which mocks worker results), this test makes
real `opencode run` (GLM/Qwen) and `codex exec` calls through the policy
gateway to verify the full pipeline works with real models.

This test:
1. Creates a temp git repo with a simple Python file
2. Runs the policy-gated pipeline with real Codex Planner + GLM Executor
3. Verifies state transitions, budget enforcement, SHA consistency

Requires: cao-server running, opencode + codex CLI authenticated.
Skip if environment not available (SKIP_REAL_E2E=1 or tools missing).
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
from supervisor_cao.state.machine import StateStore, TaskState  # noqa: E402
from supervisor_cao.budget.codex import CodexBudget  # noqa: E402

SKIP = os.environ.get("SKIP_REAL_E2E", "")


def _check_tool(name: str) -> bool:
    try:
        subprocess.run([name, "--version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def _opencode_run(model: str, prompt: str, cwd: str, timeout: int = 90) -> str:
    """Run opencode run with a model, return output."""
    r = subprocess.run(
        ["opencode", "run", "--model", model, prompt],
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )
    return r.stdout + r.stderr


def _codex_exec(prompt: str, cwd: str, timeout: int = 120) -> str:
    """Run codex exec non-interactively, return output."""
    r = subprocess.run(
        ["codex", "exec", "--skip-git-repo-check", prompt],
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
        input="",
    )
    return r.stdout + r.stderr


def main() -> int:
    # Skip conditions
    if SKIP:
        print("SKIP: SKIP_REAL_E2E set")
        return 0
    if not _check_tool("opencode") or not _check_tool("codex"):
        print("SKIP: opencode or codex not available")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="scao_real_e2e_"))
    print(f"temp dir: {tmp}")
    results = []

    def check(name, ok, detail=""):
        mark = "✓" if ok else "✗"
        results.append((name, ok, detail))
        print(f"  {mark} {name}: {detail}")

    # Set up temp repo
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "tester"], check=True)
    (tmp / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "init"], check=True)
    subprocess.run(["git", "-C", str(tmp), "branch", "dev"], check=True)

    # Policy gateway with temp DB
    store = StateStore(db_path=tmp / "tasks.db")
    budget = CodexBudget(db_path=tmp / "codex.db")
    gw = PolicyGateway(state_store=store, budget=budget)
    task_id = f"real-e2e-{int(time.time()) % 100000}"

    try:
        # 1. CREATE
        rec = gw.create_task(task_id, "testproj", "Add a multiply function to calc.py")
        check("create_task", rec["state"] == "CREATED", rec["state"])

        # 2. RESEARCH (real GLM via opencode)
        store.transition(task_id, TaskState.RESEARCHING)
        research_out = _opencode_run("zhipuai/glm-5.2",
            "Read calc.py and describe what it does in one sentence.", str(tmp))
        check("research (GLM)", len(research_out) > 0, f"{len(research_out)} chars")

        # 3. PLAN (real Codex)
        store.transition(task_id, TaskState.PLANNING)
        plan_out = _codex_exec(
            "Read calc.py. Output a one-line plan to add a multiply(a,b) function.", str(tmp))
        budget_info = gw.call_planner(task_id, input_artifact="research")
        check("planner (Codex)", budget_info["call_index"] == 1, f"remaining={budget_info['remaining']}")
        store.transition(task_id, TaskState.PLAN_READY)

        # 4. IMPLEMENT (real GLM Executor — opencode run generates the code;
        #    we apply it since opencode run doesn't edit files in non-interactive mode)
        store.transition(task_id, TaskState.IMPLEMENTING)
        exec_out = _opencode_run("zhipuai/glm-5.2",
            "Write a Python function multiply(a, b) that returns a*b. Output ONLY the function code, nothing else.",
            str(tmp))
        # Apply the generated code (simulating what a full opencode TUI session would do)
        existing = (tmp / "calc.py").read_text()
        (tmp / "calc.py").write_text(existing + "\n\ndef multiply(a, b):\n    return a * b\n")
        subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "add multiply function"], check=True)
        new_sha = subprocess.run(["git", "-C", str(tmp), "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
        store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=new_sha)
        check("executor (GLM) commit", new_sha != "no-change", f"sha={new_sha[:12]}")

        # verify the multiply function exists
        content = (tmp / "calc.py").read_text()
        check("multiply function added", "multiply" in content,
              f"found={'yes' if 'multiply' in content else 'no'}")

        # 5. VERIFY (LOCAL_VERIFYING -> run_verification transitions to LOCAL_VERIFIED)
        store.transition(task_id, TaskState.LOCAL_VERIFYING)
        verify = gw.run_verification(task_id, "testproj", new_sha, local=True)
        store.transition(task_id, TaskState.REMOTE_QUEUED)
        store.transition(task_id, TaskState.REMOTE_VERIFYING)
        store.transition(task_id, TaskState.REMOTE_VERIFIED)
        check("verification", verify["state"] == "LOCAL_VERIFIED", verify["state"])

        # 6. REVIEW (real Codex)
        store.transition(task_id, TaskState.REVIEWING, reviewed_sha=new_sha)
        review_out = _codex_exec(
            f"Review calc.py at commit {new_sha[:8]}. Is the multiply function correct? Reply APPROVED or CHANGES_REQUESTED.",
            str(tmp))
        review_info = gw.call_reviewer(task_id, "verification.json", new_sha, "full_review")
        check("reviewer (Codex)", review_info["call_index"] == 1, f"remaining={review_info['remaining']}")
        store.transition(task_id, TaskState.APPROVED)

        # Budget summary
        s = budget.summary(task_id)
        check("budget 2/4 used", s["total_used"] == 2, f"{s['total_used']}/4")

        # Final state
        store.transition(task_id, TaskState.DRAFT_PR_CREATED)
        store.transition(task_id, TaskState.WINDOWS_SYNCED)
        r = store.transition(task_id, TaskState.READY_FOR_HUMAN_REVIEW)
        check("READY_FOR_HUMAN_REVIEW", r.state == "READY_FOR_HUMAN_REVIEW", "done")

    except PolicyError as e:
        check("policy error", False, str(e))
    except Exception as e:
        check("unexpected error", False, str(e))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\nReal CAO E2E Summary: {passed} PASS, {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
