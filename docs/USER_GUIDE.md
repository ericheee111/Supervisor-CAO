# User Guide

Daily operation of Supervisor-CAO. All commands run from WSL2 Ubuntu-24.04
where `supervisor-cao` is on PATH.

## Start and diagnose

```bash
supervisor-cao up        # start cao-server (HTTP+UI on http://127.0.0.1:9889)
supervisor-cao doctor    # verify CAO, OpenCode, Codex, uv, tmux, models, pinned SHA
```

Fix any `MISSING`/`down` entry before running tasks (see `docs/TROUBLESHOOTING.md`).

## Enter a Supervisor / run a task

```bash
supervisor-cao chat pandas                       # interactive Supervisor (CAO tmux TUI)
supervisor-cao run pandas --task-file task.md    # full non-interactive pipeline
```

`chat` loads project config (local layered over example) and launches an
OpenCode Supervisor. `run` drives the full pipeline through the deterministic
policy layer; requires `cao-server` up + providers configured.

## Status and task management

```bash
supervisor-cao status                 # cao-server health + task count + recent tasks
supervisor-cao task list              # id, state, candidate/tested SHA
supervisor-cao task list --project pandas
supervisor-cao task show <task-id>    # full record + event/audit log
supervisor-cao task logs <task-id>    # per-run artifacts under ~/cao-runs/<task-id>/
supervisor-cao down                   # shut down all CAO tmux sessions
supervisor-cao upgrade                # CAO upgrade (runs regression first)
```

After a successful upgrade, re-pin the new SHA in `config/cao_pinned.sha`.

## Standard task workflow

The deterministic policy layer enforces this order — prompts only explain it,
code enforces it:

```
Research (GLM/Qwen, read-only)
  -> Codex Plan        (1 call)
  -> GLM Implement     (own worktree, commit + push task branch)
  -> WSL2 quick verify -> Qwen Verify (remote pool)
  -> Codex full Review (1 call)
  -> [CHANGES_REQUESTED] GLM Fix -> reverify -> Codex incremental Review (1 call)
  -> APPROVED -> Draft PR -> protected Windows sync (ff-only, 7 gates)
  -> READY_FOR_HUMAN_REVIEW   (terminal — NO auto-merge)
```

The platform **never auto-merges**, never updates the base branch, never force
pushes. It stops at `READY_FOR_HUMAN_REVIEW`.

States: `CREATED -> RESEARCHING -> PLANNING -> PLAN_READY -> IMPLEMENTING ->
IMPLEMENTED -> LOCAL_VERIFYING -> LOCAL_VERIFIED -> REMOTE_QUEUED ->
REMOTE_VERIFYING -> REMOTE_VERIFIED -> REVIEWING -> (CHANGES_REQUESTED -> FIXING
-> ... -> INCREMENTAL_REVIEWING) -> APPROVED -> DRAFT_PR_CREATED ->
WINDOWS_SYNCED -> READY_FOR_HUMAN_REVIEW`. Terminal failures: `FAILED`,
`NEEDS_HUMAN`, plus error states reachable from any non-terminal state.

## Task file format

YAML (or Markdown YAML front matter) validated against
`schemas/task.schema.json`. The platform refuses to guess missing performance
parameters — missing critical fields route to `NEEDS_HUMAN`.

```yaml
task_id: pandas-groupby-rolling-001
project: pandas
description: |
  Optimize Rolling.apply hot path for Cython backend; do not regress GroupBy.
base_branch: dev                       # optional, defaults to project config
baseline_sha: <git-sha>                # commit performance is measured against
benchmark_selector: "asv:benchmarks/rolling_apply.py"
performance_acceptance:
  threshold: 0.95
  direction: higher_better             # or lower_better (<= threshold passes)
regression_threshold: 0.05             # max tolerated regression vs baseline
required_test_scope:                   # test selectors that MUST be exercised
  - "pandas/tests/groupby/"
  - "pandas/tests/window/"
```

Required: `task_id`, `project`, `description`. For performance tasks the
quartet `baseline_sha`, `benchmark_selector`, `performance_acceptance`,
`required_test_scope` (plus `regression_threshold`) must be supplied at the
task level — no defaults are invented.

## Codex budget

Enforced in code (`src/supervisor_cao/budget/codex.py`), not by the Supervisor:

```yaml
max_calls_per_task: 4   # planner:1 + full_review:1 + incremental_review:1 + judge:1
```

On exhaustion the task stops with `CODEX_BUDGET_EXHAUSTED` and requires human
intervention. Codex is never spent on polling, log formatting, fixed-threshold
calculations, lint, ordinary retries, status routing, or message forwarding.

## SHA binding and disputes

- `tested_sha == candidate_sha`; `reviewed_sha == tested_sha`; any new commit
  invalidates prior verification and review. Natural-language "passed" cannot
  replace artifacts and exit codes.
- Disputes: no free-form group chat. Max sequence: `Reviewer finding -> Executor
  response (1) -> Reviewer rebuttal (1) -> Judge (1)`. No new evidence = no
  further round. Each finding needs a stable ID, severity, file/line, failure
  scenario, evidence, and recommended direction.

## See also

`docs/INSTALL.md`, `docs/ADD_PROJECT.md`, `docs/SECURITY.md`,
`docs/TROUBLESHOOTING.md`.
