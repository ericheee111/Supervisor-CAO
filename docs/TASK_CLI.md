# Task CLI Guide

## Overview

The `task` CLI commands provide a user-facing interface for running
Supervisor-CAO tasks. A task is driven through the pipeline
(research → plan → implement → verify → review) until it reaches a
terminal state: `APPROVED` (success), `FAILED`, or `NEEDS_HUMAN`.

## Commands

### task start

```bash
supervisor-cao task start \
  --repo /path/to/repo \
  --base-branch main \
  --description-file task.md \
  [--project <name>] \
  [--verify-command "pytest"] \
  [--stall-timeout 1800]
```

Starts a new task and drives it to terminal. Two modes:

- **Project mode** (`--project`): loads config from
  `~/.config/supervisor-cao/projects/<name>.local.yaml`.
- **Temp repo mode** (no `--project`): requires `--verify-command`.

The resolved config is persisted to `config-snapshot.json` for resume.

**Ctrl+C**: releases the Controller lease without killing the Worker.
The Worker continues running in the background. Resume with:

```bash
supervisor-cao task resume <task-id>
```

### task watch

```bash
supervisor-cao task watch <task-id> [--json] [--follow] [--poll-interval 5]
```

Read-only monitoring (does NOT acquire or renew the Worker lease).
Shows: current Stage, Worker type, terminal_id/pid, elapsed time,
last progress time, output summary, candidate/tested/reviewed SHA,
Codex budget.

- `--json`: output one JSON line per poll (for programmatic consumption).
- `--follow`: continuously poll until terminal.
- `--poll-interval`: poll interval in seconds (default 5).

### task resume

```bash
supervisor-cao task resume <task-id> [--stall-timeout 1800]
```

Resumes an interrupted task:
1. Reads `config-snapshot.json` (does NOT re-load mutable config).
2. Reads Worker handle from SQLite.
3. Safe takeover: checks if previous owner_pid is alive.
4. Continues driving to terminal.

### task status

```bash
supervisor-cao task status <task-id>
```

One-shot status snapshot (no polling).

### task logs

```bash
supervisor-cao task logs <task-id> [--follow]
```

Shows task log files. `--follow` tails new output (read-only).

## Worker Architecture

### Worker-shim

Workers run via `scripts/worker-shim`, an independent persistent process
that:
- Launches the Worker command (OpenCode CLI or Codex)
- Writes stdout/stderr to persistent log files
- Writes `result.json` + `exit-code` on completion
- Survives Controller exit (no daemon reaper thread)

### Two Handle Types

- **CaoTerminalHandle**: for Codex (terminal_id, session_name)
- **ProcessHandle**: for OpenCode (pid, pgid, stdout_log, stderr_log)

All handles persisted to SQLite (`workers.db`).

### Stall Detection

A Worker is marked STALLED only when ALL progress indicators are stagnant
for `stall_timeout` seconds (default 1800):
- Output not growing
- Process not alive
- CPU time not changing
- I/O counters not changing
- No child subprocesses running
- Provider last_active not updating

`PROCESSING` status counts as liveness, not progress.

### Remote Verification Mode

- `disabled`: skip remote verification (write skip artifact + audit event)
- `optional`: try remote, fallback to local on failure (default)
- `required`: remote verification must pass, fail otherwise

## Acceptance Scenarios

```bash
supervisor-cao acceptance run --scenario runtime-direct --repo /path/to/test/repo
supervisor-cao acceptance run --scenario runtime-review-fix --repo /path/to/test/repo
supervisor-cao acceptance run --scenario runtime-resume --repo /path/to/test/repo
```

These use real cao-server Workers (no fake/mock). Evidence is saved to
`acceptance/evidence/<run-id>/runtime-*/`.
