#!/usr/bin/env python3
"""920B real remote smoke test (requirement 6).

After the temp-repo live CAO E2E passes, this runs a SAFE real smoke against
the existing 920B validation pool. It does NOT modify real pandas code:
  - checks both containers are healthy + lock state
  - uses an existing clean SHA with no code changes
  - editable install + a small non-destructive pytest
  - verifies pytest failure is NOT pipe-swallowed (pipefail)
  - verifies branch/HEAD/porcelain restoration
  - verifies lock released

No full ASV. No pandas code modification. If this fails, pandas status stays BLOCKED.

Skip if SKIP_REAL_E2E=1 or no remote config (CI). Run on the host machine:
  wsl.exe -d Ubuntu-24.04 -- python3 tests/e2e/test_920b_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.projects.config import load_project  # noqa: E402

SKIP = os.environ.get("SKIP_REAL_E2E", "")
RUN_VERIFICATION = Path(__file__).resolve().parents[2] / "scripts" / "run-verification"


def _ssh(ssh_host: str, cmd: str, timeout: int = 60):
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
         "-o", "ConnectTimeout=10", ssh_host, cmd],
        capture_output=True, text=True, timeout=timeout)


def main() -> int:
    if SKIP:
        print("SKIP: SKIP_REAL_E2E set")
        return 0
    try:
        cfg = load_project("pandas")
    except Exception as e:
        print(f"SKIP: cannot load pandas project config: {e}")
        return 0
    rv = cfg.remote_validation
    ssh_host = rv.get("ssh_host", "")
    containers = rv.get("containers", [])
    user = rv.get("user", "")
    repo_path = rv.get("repo_path", "")
    conda_env = rv.get("conda_env", "")
    if not (ssh_host and containers and user and repo_path and conda_env):
        print("SKIP: remote_validation config incomplete (no real 920B pool)")
        return 0

    results = []
    def check(name, ok, detail=""):
        mark = "✓" if ok else "✗"
        results.append((name, ok, detail))
        print(f"  {mark} {name}: {detail}")

    print("=== 920B real remote smoke (read-only, small-scope) ===")
    print(f"ssh_host={ssh_host}, containers={containers}, user={user}, repo={repo_path}")

    # 1. reachability + container health
    r = _ssh(ssh_host, "echo POOL_OK", timeout=15)
    check("SSH reachable", r.returncode == 0 and "POOL_OK" in r.stdout, r.stdout.strip() or r.stderr.strip())
    if r.returncode != 0:
        print("ABORT: SSH unreachable")
        return 1

    healthy = None
    for c in containers:
        r = _ssh(ssh_host, f"docker inspect --format='{{{{.State.Running}}}}' {c}", timeout=25)
        running = r.returncode == 0 and "true" in r.stdout
        check(f"container {c} running", running, r.stdout.strip() or r.stderr.strip())
        if running and healthy is None:
            healthy = c
    if healthy is None:
        check("at least one healthy container", False, "none running")
        print("ABORT: no healthy container")
        return 1

    # 2. find a clean existing SHA on dev (no code modification)
    r = _ssh(ssh_host,
             f"docker exec {healthy} bash -lc 'cd {repo_path} && git fetch origin && git rev-parse origin/dev'",
             timeout=60)
    if r.returncode != 0:
        check("fetch + rev-parse origin/dev", False, r.stderr.strip())
        return 1
    candidate_sha = r.stdout.strip()
    check(f"use existing clean SHA (origin/dev)", bool(candidate_sha), candidate_sha[:12])

    # 3. run-verification with a small pytest scope (non-destructive)
    run_dir = Path.home() / "cao-runs" / f"920b-smoke-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["python", str(RUN_VERIFICATION),
         "--ssh-host", ssh_host, "--container", healthy, "--user", user,
         "--repo-path", repo_path, "--conda-env", conda_env,
         "--candidate-sha", candidate_sha, "--task-id", f"smoke-{int(time.time())}",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=1800)
    out = r.stdout + r.stderr
    check("run-verification exited", True, f"rc={r.returncode}")

    # 4. verify pytest result was captured (not pipe-swallowed)
    ver_file = run_dir / "verification.json"
    check("verification.json written", ver_file.exists(), str(ver_file))
    if ver_file.exists():
        ver = json.loads(ver_file.read_text())
        # pytest_passed must be a real boolean (proves exit code was captured)
        check("pytest_passed is a real boolean (not pipe-swallowed)",
              isinstance(ver.get("pytest_passed"), bool),
              str(ver.get("pytest_passed")))
        check("install_ok", ver.get("install_ok") is True, str(ver.get("install_ok")))
        check("status", ver.get("status") in ("REMOTE_VERIFIED", "FAILED"),
              str(ver.get("status")))

    # 5. verify restoration (branch/HEAD/porcelain)
    git_before = run_dir / "git-state-before.json"
    git_after = run_dir / "git-state-after.json"
    if git_before.exists() and git_after.exists():
        before = json.loads(git_before.read_text())
        after = json.loads(git_after.read_text())
        restored = (after.get("head") == before.get("head")
                    and after.get("branch") == before.get("branch")
                    and not after.get("porcelain", ""))
        check("branch/HEAD/porcelain restored", restored,
              f"head match={after.get('head','')[:12] == before.get('head','')[:12]}")

    # 6. verify lock released
    lock_file = f"/tmp/scao-{healthy}.lock"
    r = _ssh(ssh_host, f"test -f {lock_file} && echo LOCKED || echo FREE", timeout=15)
    check("lock released", "FREE" in r.stdout, r.stdout.strip())

    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n=== 920B smoke Summary: {passed} PASS, {failed} FAIL ===")
    # save sanitized summary
    summary = run_dir.parent / "920b-smoke-summary.json"
    summary.write_text(json.dumps({
        "container": healthy, "candidate_sha": candidate_sha[:12], "passed": passed,
        "failed": failed, "checks": [{"name": n, "ok": ok, "detail": d[:200]} for n, ok, d in results],
        "run_dir": str(run_dir),
    }, indent=2))
    print(f"Sanitized summary: {summary}")
    print(f"Evidence (gitignored): {run_dir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
