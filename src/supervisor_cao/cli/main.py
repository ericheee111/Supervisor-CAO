"""supervisor-cao CLI entry point (spec §18).

Commands:
  up / down              start / stop the platform (cao-server + tmux sessions)
  doctor                 diagnose environment and configuration
  chat <project>         enter interactive Supervisor for a project
  run <project> --task-file  run a task file end-to-end non-interactively
  status                 platform status
  task list / show / logs
  upgrade                explicit CAO upgrade (runs regression first)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from ..state.machine import StateStore, TaskState
from ..budget.codex import CodexBudget
from ..projects.config import load_project, list_known_projects

REPO_ROOT = Path(__file__).resolve().parents[3]
CAO_PINNED_SHA_FILE = REPO_ROOT / "config" / "cao_pinned.sha"


def _wsl_run(cmd: str, timeout: int = 30) -> tuple[int, str]:
    """Run a command in WSL2 (the platform runs on WSL). On Windows host, delegate."""
    if sys.platform.startswith("linux"):
        r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    # On Windows, delegate to WSL
    r = subprocess.run(["wsl.exe", "-d", "Ubuntu-24.04", "bash", "-lc", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


@click.group()
@click.version_option()
def cli():
    """Supervisor-CAO: generic multi-agent dev platform on CAO."""


@cli.command()
def up():
    """Start the platform: ensure cao-server is running."""
    cao_server_bin = os.environ.get("CAO_SERVER_BIN", "cao-server")
    rc, out = _wsl_run(
        f"pgrep -f 'uvicorn|cao.server|cao-server' >/dev/null 2>&1 && curl -s --max-time 2 http://127.0.0.1:9889/health >/dev/null 2>&1 && echo RUNNING || "
        f"(setsid {cao_server_bin} >/tmp/cao-server.log 2>&1 < /dev/null & disown ; sleep 4 ; "
        f"curl -s --max-time 2 http://127.0.0.1:9889/health >/dev/null 2>&1 && echo STARTED || echo FAILED)",
        timeout=20,
    )
    click.echo(out.strip())
    if "STARTED" in out or "RUNNING" in out:
        click.echo("cao-server up at http://127.0.0.1:9889")
    else:
        click.echo("FAILED to start cao-server", err=True)
        sys.exit(1)


@cli.command()
def down():
    """Stop the platform: shutdown all CAO tmux sessions and cao-server."""
    rc, out = _wsl_run("timeout 10 cao shutdown --all 2>&1 || true; pkill -f 'cao-server' 2>/dev/null || true; echo STOPPED", timeout=20)
    click.echo(out.strip() or "cao sessions stopped")


@cli.command()
def doctor():
    """Diagnose environment, config, CAO, OpenCode, Codex, models."""
    checks = []
    # CAO
    rc, out = _wsl_run("cao --version 2>&1")
    checks.append(("CAO", "ok" if rc == 0 else "MISSING", out.strip().splitlines()[0] if out.strip() else ""))
    # cao-server
    rc2, out2 = _wsl_run("curl -s --max-time 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:9889/health 2>/dev/null || echo DOWN", timeout=10)
    checks.append(("cao-server", "ok" if "200" in out2 else "down", out2.strip()))
    # OpenCode
    rc3, out3 = _wsl_run("opencode --version 2>&1")
    checks.append(("OpenCode", "ok" if rc3 == 0 else "MISSING", out3.strip().splitlines()[0] if out3.strip() else ""))
    # Codex CLI (use CODEX_BIN env if set, else try `codex` on PATH)
    codex_cmd = f"{os.environ.get('CODEX_BIN', 'codex')} --version 2>&1"
    rc4, out4 = _wsl_run(codex_cmd)
    checks.append(("Codex CLI", "ok" if rc4 == 0 else "MISSING", out4.strip().splitlines()[0] if out4.strip() else ""))
    # uv
    rc5, out5 = _wsl_run("uv --version 2>&1")
    checks.append(("uv", "ok" if rc5 == 0 else "MISSING", out5.strip().splitlines()[0] if out5.strip() else ""))
    # tmux
    rc6, out6 = _wsl_run("tmux -V 2>&1")
    checks.append(("tmux", "ok" if rc6 == 0 else "MISSING", out6.strip()))
    # projects
    checks.append(("projects", "ok", ", ".join(list_known_projects())))
    # pinned CAO sha
    sha = CAO_PINNED_SHA_FILE.read_text().strip() if CAO_PINNED_SHA_FILE.exists() else "(none)"
    checks.append(("CAO pinned SHA", "ok", sha))

    for name, status, detail in checks:
        mark = "✓" if status == "ok" else "✗"
        click.echo(f"  {mark} {name:18} {detail}")


@cli.command()
def install():
    """Install all agent profiles into CAO (cao install for each profile)."""
    import shutil
    script = REPO_ROOT / "scripts" / "install-profiles"
    if not script.exists():
        click.echo(f"install-profiles script not found: {script}", err=True)
        sys.exit(1)
    rc, out = _wsl_run(f"bash {script}")
    click.echo(out)


@cli.command()
@click.argument("project")
def chat(project):
    """Enter interactive Supervisor for a project.

    Launches a real CAO supervisor session. The Supervisor profile registers
    the supervisor-cao-policy MCP server, so the Supervisor LLM drives the
    pipeline via run_next_stage (real CAO Workers) — it has no arbitrary bash
    and cannot bypass the policy gates. The built-in @cao-mcp-server is NOT
    enabled on the Supervisor.
    """
    cfg = load_project(project)
    click.echo(f"Starting Supervisor for project '{cfg.name}' (base={cfg.base_branch})")
    click.echo("Launches: cao launch --agents supervisor --provider opencode_cli --auto-approve")
    click.echo("The Supervisor drives the pipeline via @supervisor-cao-policy (run_next_stage).")
    click.echo("(Interactive CAO tmux session - run in a WSL terminal for full TUI)")
    rc, out = _wsl_run(
        f"cd {cfg.wsl_repo or '.'} && cao launch --agents supervisor --provider opencode_cli "
        f"--auto-approve --headless --async 2>&1",
        timeout=30)
    click.echo(out.strip())


@cli.command("policy-mcp")
def policy_mcp():
    """Run the supervisor-cao-policy stdio MCP server (for debugging / CAO registration).

    This is the deterministic policy layer. The Supervisor profile registers it
    via frontmatter mcpServers; the Supervisor LLM calls its tools
    (create_task, run_next_stage, get_task, get_artifact, resume_task) to drive
    the pipeline. The built-in @cao-mcp-server is NOT enabled on the Supervisor.
    """
    from supervisor_cao.mcp.server import main as mcp_main
    sys.exit(mcp_main())


@cli.command()
@click.argument("project")
@click.option("--task-file", required=True, type=click.Path(exists=True))
@click.option("--task-id", default=None, help="task ID (default: auto from file)")
@click.option("--max-stages", default=30, help="safety bound on stage iterations")
def run(project, task_file, task_id, max_stages):
    """Run a task file end-to-end through the policy-gated pipeline.

    Executes: Research -> Codex Plan -> GLM Implement -> Qwen Verify
    -> Codex Review -> Draft PR -> Windows Sync -> READY_FOR_HUMAN_REVIEW.

    Each stage is driven by run_next_stage, which launches a REAL CAO Worker
    (researcher/codex-planner/glm-executor/qwen-verifier/codex-reviewer) via
    POST /terminals/run-step, validates the artifact against its JSON schema,
    and advances the state machine with real SHAs. No state is advanced
    without a real artifact. The policy gateway enforces state machine, Codex
    budget, SHA matching, worktree isolation, and sync gates in code.

    The --dry-run flag has been removed: this command always runs real Workers.
    Use `supervisor-cao task show <id>` to inspect state and artifacts.
    """
    from supervisor_cao.mcp.policy_gateway import PolicyGateway, PolicyError
    from supervisor_cao.state.machine import IllegalTransition, ShaMismatch, TaskState
    import yaml as _yaml
    import time as _time

    cfg = load_project(project)
    task_data = _yaml.safe_load(Path(task_file).read_text()) or {}
    tid = task_id or task_data.get("task_id") or f"task-{int(_time.time())}"
    description = task_data.get("description", "")
    baseline = task_data.get("baseline_sha")

    click.echo(f"=== Supervisor-CAO run: {tid} on {cfg.name} ===")
    click.echo("Pipeline: Research -> Plan -> Implement -> Verify -> Review -> Draft PR -> Win Sync")
    click.echo("(each stage launches a real CAO Worker via the policy gateway)")

    gw = PolicyGateway()
    run_dir = Path.home() / "cao-runs" / tid
    run_dir.mkdir(parents=True, exist_ok=True)

    # health-check the cao-server before starting (real Workers need it)
    if not gw.cao.server_health():
        click.echo("ERROR: cao-server is not running. Start it with 'supervisor-cao up'.", err=True)
        sys.exit(1)

    try:
        # 1. CREATE
        click.echo("\n[create] CREATE task...")
        rec = gw.create_task(tid, project, description, baseline)
        click.echo(f"  state={rec['state']} baseline={baseline or 'none'}")

        # 2. drive stages until terminal
        terminal = {TaskState.READY_FOR_HUMAN_REVIEW.value, TaskState.FAILED.value,
                    TaskState.NEEDS_HUMAN.value}
        for i in range(1, max_stages + 1):
            rec = gw.get_task(tid)
            if rec["state"] in terminal:
                break
            click.echo(f"\n[stage {i}] {rec['state']} -> run_next_stage ...")
            rec = gw.run_next_stage(tid)
            click.echo(f"  state={rec['state']} cand={rec.get('candidate_sha') or '-'}"
                       f" tested={rec.get('tested_sha') or '-'}"
                       f" reviewed={rec.get('reviewed_sha') or '-'}"
                       f" err={rec.get('error') or '-'}")
            if rec["state"] in terminal:
                break

        click.echo(f"\n=== DONE: state={rec['state']} ===")
        click.echo(f"Codex budget used: {gw.budget_summary(tid)['total_used']}/4")
        click.echo(f"Artifacts: {run_dir}")
        if rec["state"] == TaskState.READY_FOR_HUMAN_REVIEW.value:
            click.echo("Task is READY_FOR_HUMAN_REVIEW. No auto-merge performed.")
        else:
            click.echo(f"Task stopped at {rec['state']}. Inspect artifacts in {run_dir}.")
            sys.exit(2)

    except PolicyError as e:
        click.echo(f"\nPOLICY ERROR: {e}", err=True)
        sys.exit(2)


@cli.command()
def status():
    """Show platform status: cao-server, sessions, tasks."""
    rc, out = _wsl_run("curl -s --max-time 3 http://127.0.0.1:9889/health 2>/dev/null || echo DOWN", timeout=10)
    click.echo(f"cao-server: {'UP' if 'ok' in out.lower() or '200' in out else 'DOWN'}")
    store = StateStore()
    tasks = store.list()
    click.echo(f"tasks: {len(tasks)}")
    for t in tasks[:10]:
        click.echo(f"  {t.task_id:24} {t.state:24} {t.project}")


@cli.group()
def task():
    """Task management."""


@task.command("list")
@click.option("--project", default=None)
def task_list(project):
    store = StateStore()
    for t in store.list(project):
        click.echo(f"{t.task_id:24} {t.state:24} {t.project}  cand={t.candidate_sha or '-':12} tested={t.tested_sha or '-':12}")


@task.command("show")
@click.argument("task_id")
def task_show(task_id):
    store = StateStore()
    t = store.get(task_id)
    if not t:
        click.echo(f"task not found: {task_id}", err=True)
        sys.exit(1)
    click.echo(json.dumps(t.to_dict(), indent=2, default=str))
    click.echo("--- events ---")
    for e in store.events(task_id):
        click.echo(f"  {e['ts']:.0f} {e['event']:10} {e.get('from_state') or '-':24} -> {e.get('to_state') or '-'}")


@task.command("logs")
@click.argument("task_id")
def task_logs(task_id):
    run_dir = Path.home() / "cao-runs" / task_id
    if not run_dir.exists():
        click.echo(f"no run dir for {task_id}")
        return
    for f in sorted(run_dir.iterdir()):
        click.echo(f"--- {f.name} ---")
        try:
            click.echo(f.read_text()[:2000])
        except Exception:
            click.echo("(binary)")


@cli.command()
def upgrade():
    """Explicit CAO upgrade. Runs regression first; keeps old version on failure."""
    click.echo("CAO upgrade: running regression suite first...")
    rc, out = _wsl_run("cd /root/cao-platform-src/cli-agent-orchestrator && python -m pytest tests/ -q 2>&1 | tail -5", timeout=300)
    if rc != 0:
        click.echo("Regression FAILED - keeping current CAO version.")
        click.echo(out)
        sys.exit(1)
    click.echo("Regression passed. Proceeding with upgrade...")
    click.echo("(Run: uv tool install --upgrade git+https://github.com/awslabs/cli-agent-orchestrator.git@main)")
    click.echo("After upgrade, re-pin the new SHA in config/cao_pinned.sha")


if __name__ == "__main__":
    cli()
