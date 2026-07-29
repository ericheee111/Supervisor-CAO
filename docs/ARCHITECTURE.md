# Architecture

Supervisor-CAO is a generic, safety-first multi-agent software-development
platform built on [AWS Labs CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator).

## Design principle

**Deterministic code enforces policy; prompts only explain it.**

Budgets, locks, SHA matching, state transitions, sync gates, and permissions
are enforced in Python code (`src/supervisor_cao/`), never in LLM prompts.
The Supervisor may *describe* a rule, but only the policy layer can *enforce* it.

## High-level flow

```
User
  ↓
OpenCode + GLM/Qwen Supervisor
  ↓
Deterministic policy layer (state machine, budgets, SHA, locks, gates)
  ├── Researcher        (GLM/Qwen, read-only)
  ├── Codex Planner     (read-only, paid, 1 call)
  ├── GLM Executor      (writable, own worktree)
  ├── Qwen Verifier     (read-only tests, remote pool)
  ├── Codex Reviewer    (read-only, paid, 1 call)
  └── Codex Judge       (read-only, paid, disputes only)
```

Standard task lifecycle:

```
Research → Codex Plan → GLM Implement → WSL2 quick verify
→ 920B remote verify → Qwen report → Codex full Review
→ (if CHANGES_REQUESTED: GLM Fix → reverify → Codex incremental Review)
→ APPROVED → Draft PR → Windows sync → READY_FOR_HUMAN_REVIEW
```

The platform never auto-merges. It stops at `READY_FOR_HUMAN_REVIEW`.

## CAO integration

CAO runs a local `cao-server` (HTTP API + Web UI on port 9889) and launches
provider CLIs (OpenCode, Codex) in isolated tmux sessions. The Supervisor uses
CAO's `assign` / `handoff` / `send_message` MCP tools to delegate to workers.

- **OpenCode provider** (experimental): multi-agent callback uses inbox polling
  fallback (CAO issues #203/#115). Long-task message delivery is tested
  separately. CAO isolates OpenCode config at `~/.aws/opencode/`, separate from
  the user's personal `~/.config/opencode/`.
- **Codex CLI provider**: used non-interactively (`codex exec`) for read-only
  Plan/Review/Judge. ChatGPT Pro auth.

CAO is pinned to a specific commit (`config/cao_pinned.sha`). Upgrades are
explicit (`supervisor-cao upgrade`) and run a regression suite first.

## Policy layer components

### State machine (`src/supervisor_cao/state/machine.py`)

SQLite-backed task state store. Enforces:
- Legal forward transitions only (no skipping states).
- SHA matching: `tested_sha == candidate_sha`; `reviewed_sha == tested_sha`.
- Any new `candidate_sha` invalidates `tested_sha` and `reviewed_sha`.
- Gate checks before terminal-success states (LOCAL_VERIFIED, REMOTE_VERIFIED,
  APPROVED, DRAFT_PR_CREATED require SHA consistency).
- Error states (LOCAL_WORKTREE_DIRTY, CODEX_BUDGET_EXHAUSTED, NO_PROGRESS,
  WINDOWS_SYNC_BLOCKED, etc.) reachable from any non-terminal state.
- Full audit log (events table).

States: CREATED → RESEARCHING → PLANNING → PLAN_READY → IMPLEMENTING →
IMPLEMENTED → LOCAL_VERIFYING → LOCAL_VERIFIED → REMOTE_QUEUED →
REMOTE_VERIFYING → REMOTE_VERIFIED → REVIEWING → (CHANGES_REQUESTED → FIXING →
... → INCREMENTAL_REVIEWING) → APPROVED → DRAFT_PR_CREATED → WINDOWS_SYNCED →
READY_FOR_HUMAN_REVIEW. Plus FAILED / NEEDS_HUMAN terminals.

### Codex budget (`src/supervisor_cao/budget/codex.py`)

Per-task, per-role budget. Max 4 calls: planner(1) + full_review(1) +
incremental_review(1) + judge(1). Atomic spend under lock. Raises
`BudgetExhausted` (`CODEX_BUDGET_EXHAUSTED`) when exceeded. Persists call log
with input/output artifacts, candidate SHA, timestamps.

### Worktree management (`src/supervisor_cao/workers/worktrees.py`)

Per-task isolated worktrees: `~/cao-worktrees/<project>/<task-id>/{executor,verifier,reviewer}`.
- executor: writable, on `agent/<task-id>` branch
- verifier/reviewer: read-only checkouts
- main clone only for fetch/branch/worktree mgmt, never edited
- no force push, no base-branch rewrite, every valid candidate committed+pushed

### Remote validation pool (`src/supervisor_cao/validation/remote_pool.py`)

920B dual-container pool over SSH. Atomic lock (remote flock/mkdir). Records
original branch/HEAD before, refuses dirty repos, restores after (no
`reset --hard`, no `clean -fdx`). Marks UNHEALTHY on restore failure.
Supervisor only reads: AVAILABLE / BUSY / UNHEALTHY / DIRTY / UNREACHABLE.

### Windows sync (`src/supervisor_cao/validation/windows_sync.py`)

Fast-forward only sync to the Windows repo. 7 gates must ALL pass:
candidate_pushed, tested_eq_candidate, reviewed_eq_candidate, review_approved,
draft_pr_created, windows_clean, fast_forwardable. Never reset --hard, never
overwrite dirty, never force checkout, never cherry-pick, never merge dev.
Final check: `Windows HEAD == candidate SHA`.

### Project config (`src/supervisor_cao/projects/config.py`)

Layered: public example (`config/examples/<project>.example.yaml`) + private
local (`~/.config/supervisor-cao/projects/<project>.local.yaml`) + task
override. Never hard-codes project specifics.

## Role permissions

| Role | Source access | Git | Codex budget |
|------|--------------|-----|--------------|
| Supervisor | read task state + artifacts | none (orchestration only) | none |
| Researcher | read-only | read-only | none |
| Codex Planner | read-only | read-only | 1 planner |
| GLM Executor | own worktree only | commit+push task branch | none |
| Qwen Verifier | read-only + scripts | none | none |
| Codex Reviewer | read-only | read-only | 1 full_review (+1 incremental) |
| Codex Judge | read-only | read-only | 1 judge (disputes only) |

## Public vs local data

Public repo: generic code, profiles (no secrets), schemas, sanitized examples,
tests, docs, CAO pinned SHA.

Local only (`~/.config/supervisor-cao/`, `~/.local/state/supervisor-cao/`,
`~/cao-runs/`): API keys, SSH hosts, container names, usernames, paths, run
logs, models.local.yaml, *.local.yaml, secrets.env.

Every push runs `scripts/scan-secrets` to block leaks.
