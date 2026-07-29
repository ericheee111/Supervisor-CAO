# Supervisor-CAO

A generic, safety-first multi-agent software-development platform built on
[AWS Labs CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator).

Supervisor-CAO coordinates multiple AI coding CLIs so a cheap GLM/Qwen
Supervisor can delegate work to specialist agents, calling Codex only for
high-value tasks (planning, full review, incremental review, dispute
arbitration). Deterministic code — not prompts — enforces budgets, SHA
matching, locks, state transitions, and sync gates.

> **ZCode + GLM 5.2 only bootstraps this platform. It is NOT part of the runtime
> agent team.**

## Architecture

```
User
  ↓
OpenCode + GLM/Qwen Supervisor
  ↓
Deterministic policy layer (state machine, budgets, SHA, locks, sync gates)
  ├── Researcher        (GLM/Qwen, read-only)
  ├── Codex Planner     (read-only, paid)
  ├── GLM Executor      (writable, own worktree)
  ├── Qwen Verifier     (read-only tests, remote pool)
  ├── Codex Reviewer    (read-only, paid)
  └── Codex Judge       (read-only, paid, disputes only)
```

## Cost strategy

- **GLM/Qwen**: persistent supervision, high-frequency implementation, testing,
  diagnostics, report compression.
- **Codex**: low-frequency Plan, full Review, incremental Review, dispute
  arbitration. Max 4 calls per task, enforced in code.
- **Deterministic code**: state machine, budgets, SHA checks, locks, sync,
  safety gates.

## Status

Platform stops at `READY_FOR_HUMAN_REVIEW`. No auto-merge. No base-branch
changes. No force push.

### Verification results

| Test | Result |
|------|--------|
| Unit tests (state machine, budget, schemas, SHA, locks, windows sync, config, secret scan) | 51 passed |
| Integration tests (planner, executor fix, verifier fail, stale, budget, pool, windows blocked, happy path) | 10 passed |
| E2E (temp-repo full flow: state + budget + worktree + commit + push + verify + review + draft PR + windows sync) | 13/13 passed |
| Stability (10 consecutive E2E runs) | 10/10 passed |
| Supervisor benchmark (GLM 5.2 + Qwen 3.7 Max) | both 4/4, Qwen primary (faster) |
| pandas read-only smoke | 3 PASS (config, origin/dev, Windows repo dirty-detect) |
| `supervisor-cao doctor` | CAO/OpenCode/Codex/uv/tmux/projects/pinned-SHA all ✓ |
| Secret scan (pre-push) | clean, no private files tracked |

### Known limitations

- **kpserver SSH not configured**: remote validation pool (2 containers, conda,
  pool locks) is `LIMITATION`. The deterministic `remote_pool.py` and
  `run-verification` scripts are implemented and unit-tested, but the live SSH
  alias must be configured in `~/.ssh/config` to reach the 920B pool.
- **WSL2 network restricted**: iKuuuVPN TUN hijacks DNS (fake-ip) and blocks
  direct github/pypi. CAO was installed offline via a local wheelhouse
  (`uv tool install --offline --find-links`). `supervisor-cao upgrade` requires
  network or a refreshed wheelhouse.
- **Codex CLI on Windows path**: Codex CLI (`codex.exe`) lives in the Codex
  Desktop install dir. Set `CODEX_BIN` env var to its path, or symlink it.
- **CAO OpenCode provider experimental**: multi-agent callback uses inbox
  polling fallback (CAO issues #203/#115). Long-task message delivery and
  recovery need live CAO multi-agent testing before unattended runs.
- **Supervisor benchmark is a capability probe**: full CAO `handoff`/`assign`/
  `send_message` multi-agent testing requires a live `cao-server` + worker
  sessions, not just `opencode run`.

## Quick start

```bash
# 1. Ensure WSL2 Ubuntu-24.04 with CAO, OpenCode, Codex CLI installed.
# 2. Start the platform
supervisor-cao up

# 3. Diagnose
supervisor-cao doctor

# 4. Enter a Supervisor for a project
supervisor-cao chat pandas

# 5. Status / tasks
supervisor-cao status
supervisor-cao task list

# 6. Stop
supervisor-cao down
```

See `docs/INSTALL.md` for full setup and `docs/USER_GUIDE.md` for usage.

## Repository layout

```
Supervisor-CAO/
├── bin/supervisor-cao          # launcher
├── profiles/                   # agent profiles (no secrets)
├── config/                     # sanitized examples + pinned CAO SHA
├── schemas/                    # JSON schemas for agent artifacts
├── scripts/                    # detect-models, manage-worktrees, remote-pool, ...
├── src/supervisor_cao/         # deterministic policy layer
│   ├── state/                  # task state machine + SQLite store
│   ├── budget/                 # Codex call budget
│   ├── projects/               # project config loader
│   ├── workers/                # worktree management
│   ├── validation/             # remote pool + Windows sync
│   └── cli/                    # supervisor-cao CLI
├── tests/                      # unit, integration, e2e
└── docs/                       # INSTALL, USER_GUIDE, SECURITY, ...
```

## Private data isolation

Machine-specific data (API keys, SSH hosts, container names, usernames, paths)
lives in `~/.config/supervisor-cao/` and is never committed. See
`docs/SECURITY.md`.

## CAO version

CAO is pinned to a specific commit (see `config/cao_pinned.sha`). Upgrades are
explicit (`supervisor-cao upgrade`) and run a regression suite first.

## License

Apache-2.0
