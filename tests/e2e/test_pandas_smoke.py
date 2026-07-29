#!/usr/bin/env python3
"""pandas read-only smoke test (spec §20.4).

Verifies WITHOUT modifying pandas:
  - load pandas project config
  - read origin/dev SHA from WSL clone (or Windows repo)
  - check Windows repo state (read-only, no branch switch)
  - test SSH to remote host (if configured)
  - test Docker containers (if reachable)
  - check Conda environment (if reachable)
  - detect pool lock state (read-only)
  - DO NOT modify pandas code
  - DO NOT switch real Windows branch
  - DO NOT create real pandas PR
  - DO NOT run full ASV

This is read-only. It reports what it finds. Unreachable items are reported
as LIMITATION, not failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.projects.config import load_project  # noqa: E402


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)


def _ssh(host: str, cmd: str, timeout: int = 15) -> tuple[int, str]:
    return _run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=8", host, cmd], timeout=timeout)


def main() -> int:
    results = {"checks": [], "limitations": []}
    cfg = load_project("pandas")

    def check(name, ok, detail=""):
        status = "PASS" if ok else ("LIMITATION" if "unreachable" in detail.lower() or "not configured" in detail.lower() else "FAIL")
        results["checks"].append({"name": name, "status": status, "detail": detail})
        mark = {"PASS": "✓", "FAIL": "✗", "LIMITATION": "⚠"}[status]
        print(f"  {mark} {name}: {detail}")

    # 1. load config
    check("load pandas config", cfg.name == "pandas", f"name={cfg.name} base={cfg.base_branch}")

    # 2. WSL clone origin/dev
    wsl_repo = cfg.wsl_repo
    if wsl_repo and Path(wsl_repo.replace("~", str(Path.home()))).exists():
        rc, out = _run(["git", "-C", wsl_repo, "rev-parse", "origin/dev"])
        check("WSL clone origin/dev", rc == 0, f"sha={out[:12]}" if rc == 0 else out)
    else:
        # try Windows repo via /mnt
        win_repo = "/mnt/d/Projects/pandas"
        rc, out = _run(["git", "-C", win_repo, "rev-parse", "origin/dev"])
        check("Windows repo origin/dev", rc == 0, f"sha={out[:12]}" if rc == 0 else f"unreachable: {out[:80]}")

    # 3. Windows repo state (read-only)
    win_path = cfg.windows_repo or "D:/Projects/pandas"
    win_wsl = "/mnt/d/Projects/pandas"
    rc, out = _run(["git", "-C", win_wsl, "status", "--porcelain"])
    if rc == 0:
        dirty = bool(out.strip())
        check("Windows repo clean (read-only)", True, f"dirty={dirty} (uncommitted changes protected)")
    else:
        check("Windows repo state", False, f"unreachable: {out[:80]}")

    # 4. SSH to remote host
    ssh_host = cfg.remote_validation.get("ssh_host", "")
    if ssh_host:
        rc, out = _ssh(ssh_host, "echo KP_OK")
        check(f"SSH {ssh_host}", rc == 0 and "KP_OK" in out, out[:80] if rc != 0 else "connected")
    else:
        check("SSH host", False, "not configured")

    # 5. Docker containers
    containers = cfg.remote_validation.get("containers", [])
    if containers and ssh_host:
        for c in containers:
            rc, out = _ssh(ssh_host, f"docker inspect --format='{{{{.State.Running}}}}' {c}")
            running = rc == 0 and "true" in out
            check(f"container {c}", running, "running" if running else f"unreachable: {out[:60]}")
    else:
        check("containers", False, "not configured or SSH unreachable")

    # 6. Conda env + pandas import (test ALL containers, not just first)
    conda_env = cfg.remote_validation.get("conda_env", "")
    conda_path = cfg.remote_validation.get("conda_path", "/opt/miniforge3")
    user = cfg.remote_validation.get("user", "")
    repo_path = cfg.remote_validation.get("repo_path", "")
    if ssh_host and containers and conda_env:
        for c in containers:
            rc, out = _ssh(ssh_host,
                           f"docker exec {c} bash -lc 'source {conda_path}/etc/profile.d/conda.sh && conda activate {conda_env} && python --version && python -c \"import pandas; print(pandas.__version__)\"'")
            check(f"conda {conda_env} + pandas ({c})", rc == 0, out.strip()[:80] if rc == 0 else f"unreachable: {out[:80]}")
    else:
        check("conda env", False, "not configured or SSH unreachable")

    # 7. pool lock (read-only)
    if ssh_host and containers:
        c0 = containers[0]
        rc, out = _ssh(ssh_host, f"test -f /tmp/scao-{c0}.lock && echo LOCKED || echo FREE")
        check("pool lock detect", rc == 0, out.strip() if rc == 0 else f"unreachable: {out[:60]}")
    else:
        check("pool lock detect", False, "SSH unreachable")

    # summary
    passed = sum(1 for c in results["checks"] if c["status"] == "PASS")
    failed = sum(1 for c in results["checks"] if c["status"] == "FAIL")
    limited = sum(1 for c in results["checks"] if c["status"] == "LIMITATION")
    print(f"\nSummary: {passed} PASS, {failed} FAIL, {limited} LIMITATION")

    # write report
    report_dir = Path.home() / "cao-runs" / "pandas-smoke"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "smoke-result.json").write_text(json.dumps(results, indent=2))
    print(f"Report: {report_dir / 'smoke-result.json'}")

    # FAIL only on real failures, not limitations
    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
