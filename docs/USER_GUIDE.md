[English](#user-guide) | [简体中文](#用户指南)

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
supervisor-cao chat demo-project                 # interactive Supervisor (CAO tmux TUI)
supervisor-cao run demo-project --task-file task.md    # full non-interactive pipeline
```

`chat` loads project config (local layered over example) and launches an
OpenCode Supervisor. `run` drives the full pipeline through the deterministic
policy layer; requires `cao-server` up + providers configured.

## Status and task management

```bash
supervisor-cao status                 # cao-server health + task count + recent tasks
supervisor-cao task list              # id, state, candidate/tested SHA
supervisor-cao task list --project demo-project
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
task_id: demo-project-feature-001
project: demo-project
description: |
  Implement and verify a small feature in the demo-project codebase without
  regressing existing behavior.
base_branch: main                      # optional, defaults to project config
baseline_sha: <git-sha>                # commit performance is measured against
benchmark_selector: "demo:benchmarks/feature_bench.py"
performance_acceptance:
  threshold: 0.95
  direction: higher_better             # or lower_better (<= threshold passes)
regression_threshold: 0.05             # max tolerated regression vs baseline
required_test_scope:                   # test selectors that MUST be exercised
  - "tests/unit/"
  - "tests/integration/"
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

---

# 用户指南

Supervisor-CAO 的日常操作。所有命令都在 WSL2 Ubuntu-24.04 中运行，且
`supervisor-cao` 已在 PATH 上。

## 启动与诊断

```bash
supervisor-cao up        # 启动 cao-server（HTTP+UI 在 http://127.0.0.1:9889）
supervisor-cao doctor    # 验证 CAO、OpenCode、Codex、uv、tmux、模型、固定 SHA
```

在运行任务前，修复任何 `MISSING`/`down` 条目（见 `docs/TROUBLESHOOTING.md`）。

## 进入 Supervisor / 运行任务

```bash
supervisor-cao chat demo-project                 # 交互式 Supervisor（CAO tmux TUI）
supervisor-cao run demo-project --task-file task.md    # 完整非交互式流水线
```

`chat` 加载项目配置（本地配置叠加在示例之上）并启动一个 OpenCode
Supervisor。`run` 通过确定性策略层驱动完整流水线；要求 `cao-server` 已
启动且提供方已配置。

## 状态与任务管理

```bash
supervisor-cao status                 # cao-server 健康 + 任务计数 + 最近任务
supervisor-cao task list              # id、状态、candidate/tested SHA
supervisor-cao task list --project demo-project
supervisor-cao task show <task-id>    # 完整记录 + 事件/审计日志
supervisor-cao task logs <task-id>    # ~/cao-runs/<task-id>/ 下的每次运行产物
supervisor-cao down                   # 关闭所有 CAO tmux 会话
supervisor-cao upgrade                # CAO 升级（先运行回归测试）
```

成功升级后，在 `config/cao_pinned.sha` 中重新固定新的 SHA。

## 标准任务工作流

确定性策略层强制执行此顺序 — prompt 只是解释它，代码强制执行它：

```
Research（GLM/Qwen，只读）
  -> Codex Plan        (1 次调用)
  -> GLM Implement     (自有 worktree，commit + push task 分支)
  -> WSL2 快速验证 -> Qwen Verify（远程池）
  -> Codex 完整 Review (1 次调用)
  -> [CHANGES_REQUESTED] GLM Fix -> 重新验证 -> Codex 增量 Review (1 次调用)
  -> APPROVED -> Draft PR -> 受保护的 Windows 同步（ff-only，7 道门禁）
  -> READY_FOR_HUMAN_REVIEW   （终态 — 不自动合并）
```

该平台**绝不自动合并**，绝不更新 base 分支，绝不强制推送。它在
`READY_FOR_HUMAN_REVIEW` 处停止。

状态：`CREATED -> RESEARCHING -> PLANNING -> PLAN_READY -> IMPLEMENTING ->
IMPLEMENTED -> LOCAL_VERIFYING -> LOCAL_VERIFIED -> REMOTE_QUEUED ->
REMOTE_VERIFYING -> REMOTE_VERIFIED -> REVIEWING -> (CHANGES_REQUESTED -> FIXING
-> ... -> INCREMENTAL_REVIEWING) -> APPROVED -> DRAFT_PR_CREATED ->
WINDOWS_SYNCED -> READY_FOR_HUMAN_REVIEW`。终态失败：`FAILED`、
`NEEDS_HUMAN`，以及从任何非终态可达的错误状态。

## 任务文件格式

YAML（或 Markdown YAML front matter），依据 `schemas/task.schema.json`
校验。平台拒绝猜测缺失的性能参数 — 缺失关键字段会路由到 `NEEDS_HUMAN`。

```yaml
task_id: demo-project-feature-001
project: demo-project
description: |
  在 demo-project 代码库中实现并验证一个小特性，且不回归现有行为。
base_branch: main                      # 可选，默认来自项目配置
baseline_sha: <git-sha>                # 性能测量基准的 commit
benchmark_selector: "demo:benchmarks/feature_bench.py"
performance_acceptance:
  threshold: 0.95
  direction: higher_better             # 或 lower_better（<= threshold 即通过）
regression_threshold: 0.05             # 相对 baseline 容忍的最大回归
required_test_scope:                   # 必须执行到的测试选择器
  - "tests/unit/"
  - "tests/integration/"
```

必填：`task_id`、`project`、`description`。对于性能任务，四元组
`baseline_sha`、`benchmark_selector`、`performance_acceptance`、
`required_test_scope`（加上 `regression_threshold`）必须在任务级别提供
— 不会发明默认值。

## Codex 预算

在代码中强制执行（`src/supervisor_cao/budget/codex.py`），而非由 Supervisor
执行：

```yaml
max_calls_per_task: 4   # planner:1 + full_review:1 + incremental_review:1 + judge:1
```

耗尽时任务以 `CODEX_BUDGET_EXHAUSTED` 停止，需要人工介入。Codex 绝不用于
轮询、日志格式化、固定阈值计算、lint、普通重试、状态路由或消息转发。

## SHA 绑定与争议

- `tested_sha == candidate_sha`；`reviewed_sha == tested_sha`；任何新 commit
  都会使之前的验证和 review 失效。自然语言的 "passed" 不能替代产物和
  退出码。
- 争议：没有自由形式的群聊。最大序列：`Reviewer finding -> Executor
  response (1) -> Reviewer rebuttal (1) -> Judge (1)`。没有新证据 = 没有
  下一轮。每个 finding 需要一个稳定的 ID、严重级别、文件/行、失败场景、
  证据和推荐方向。

## 另请参阅

`docs/INSTALL.md`、`docs/ADD_PROJECT.md`、`docs/SECURITY.md`、
`docs/TROUBLESHOOTING.md`。
