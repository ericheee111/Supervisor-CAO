[English](#architecture) | [简体中文](#架构)

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
→ remote pool verify → Qwen report → Codex full Review
→ (if CHANGES_REQUESTED: GLM Fix → reverify → Codex incremental Review)
→ APPROVED → Draft PR → Windows sync → READY_FOR_HUMAN_REVIEW
```

The platform never auto-merges. It stops at `READY_FOR_HUMAN_REVIEW`.

## Project adapter and validation backend (`src/supervisor_cao/projects/adapter.py`)

The platform core never hard-codes a project name, base branch, test runner, or
model id. Everything comes from a `ProjectConfig` and two generic interfaces:

- **`ProjectAdapter`**: exposes base branch, task-branch template, worktree
  root, and repo paths from project config. The core asks the adapter; it never
  assumes them.
- **`ValidationBackend`**: runs local and remote verification via configured
  commands/plugins (`run_local` / `run_remote`). The core only reads the exit
  code, logs, SHA, and structured result. The backend CANNOT change pass/fail —
  that is the exit code's job. The model only summarizes the result.

`local_fixture` is a test-only marker: a backend flagged as a local fixture MAY
simulate verification in tests, but production code must never write a simulated
result into `REMOTE_VERIFIED`. `test_mode` is injected via
`PolicyGateway(test_mode=True)` — there is no `.test-mode` file.

## Configurable verification commands

Verification commands are NOT hard-coded to any test runner, benchmark suite, or
environment manager. `scripts/run-verification` runs remote verification as a
single try/finally transaction (select container → acquire owner lock → check
clean → record git state → checkout candidate → run commands → restore git
state → release lock) and accepts:

- `--verify-command 'CMD'` (repeatable) — one or more shell commands run inside
  the container,
- `--verify-script FILE` — a script file run inside the container, or
- `--setup-command 'CMD'` — a one-shot setup command before verification.

If none are passed, the project config's `default_verification.remote` steps
are used. The platform core only reads this script's exit code and the
structured `verification.json` it writes.

## StageStore and candidate_sha invalidation (`src/supervisor_cao/mcp/stage_store.py`)

Each stage of a task is recorded in SQLite with its status, the CAO
`terminal_id`, its artifact, the `candidate_sha` at the time, and the Codex
budget call id (if any). Resume rules (enforced in code):

- A **COMPLETED** stage with the **SAME** `candidate_sha` is never re-run on
  resume — the Worker is not re-launched, Codex budget is not re-spent, no
  duplicate commit/PR/Windows-sync happens.
- A **COMPLETED** stage with a **DIFFERENT** `candidate_sha` (a fix produced a
  new SHA) is **stale and MUST be re-run** — re-verification and incremental
  review are mandatory.
- A stale **RUNNING** record (older than the staleness TTL) is reclaimed and a
  new run begins; a live RUNNING record is reused.

The Executor verifies, before accepting a candidate, that its SHA differs from
the base SHA, that a real diff exists, that it is on the correct task branch,
and that it has been pushed to the remote.

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

### WorkerMonitor (`src/supervisor_cao/workers/monitor.py`)

Wraps each worker process in a **dual-handle model**: a `CaoTerminalHandle`
(the CAO tmux session id) plus a `ProcessHandle` (the OS-level CLI process).
The two are correlated but tracked independently so a dead process is
detectable even when the CAO terminal record still claims RUNNING.

Deterministic interface: `start_worker` / `poll_worker` / `wait_for_stage` /
`resume_worker`. Polling compares process liveness against recorded stage
progress; it never trusts the worker's self-reported status alone.

Stall detection: a worker with no stage progress within `stall_timeout`
(default `1800`s = 30 min) is marked `STALLED`. `STALLED` is a **handle
status**, not a `TaskState` — the underlying task keeps its current state
and may be resumed by a fresh worker via `resume_worker`.

Concurrent ownership: each handle carries `owner_id`, `lease_until`, and
`heartbeat`; only the current lease holder may poll or resume. A lease
expires at `lease_until`; a new owner may take over only after expiry.

`max_runtime` defaults to `null` — there is **no total runtime timeout**.
Monitoring is progress-based (stall detection), not wall-clock-based: a
worker that keeps advancing stage progress is never killed for running
"too long".

### Remote validation pool (`src/supervisor_cao/validation/remote_pool.py`)

Generic remote validation pool, managed over SSH with atomic locks (remote
flock/mkdir). Records original branch/HEAD before, refuses dirty repos,
restores after (no `reset --hard`, no `clean -fdx`). Marks UNHEALTHY on restore
failure. Supervisor only reads: AVAILABLE / BUSY / UNHEALTHY / DIRTY /
UNREACHABLE. The actual host names, container names, users, and paths are
private (local config only); the core treats the pool opaquely.

### Windows sync (`src/supervisor_cao/validation/windows_sync.py`)

Fast-forward only sync to the Windows repo. 7 gates must ALL pass:
candidate_pushed, tested_eq_candidate, reviewed_eq_candidate, review_approved,
draft_pr_created, windows_clean, fast_forwardable. Never reset --hard, never
overwrite dirty, never force checkout, never cherry-pick, never merge dev.
Final check: `Windows HEAD == candidate SHA`.

### Judge arbitration (`src/supervisor_cao/state/judge.py`)

When the Reviewer returns `CHANGES_REQUESTED`, all findings — not just the
disputed ones — go to the Judge. The Judge emits one of four verdicts:

- **`OVERTURN`** — every Reviewer finding is overturned → the task moves
  straight to `APPROVED`.
- **`UPHOLD`** — every finding is upheld → back to `FIXING`.
- **`MIXED`** — some overturned, some upheld → back to `FIXING` for the
  upheld findings only.
- **`UNRESOLVED`** — the Judge cannot decide → `NEEDS_HUMAN`.

Promotion rule: **only an all-`OVERTURN` verdict promotes to `APPROVED`**.
`UPHOLD`, `MIXED`, and `UNRESOLVED` all route away from `APPROVED` —
`UPHOLD`/`MIXED` to `FIXING`, `UNRESOLVED` to `NEEDS_HUMAN`. The Judge never
auto-merges; it only arbitrates the Reviewer's findings.

### Project config (`src/supervisor_cao/projects/config.py`)

Layered: public example (`config/examples/<project>.example.yaml`) + private
local (`~/.config/supervisor-cao/projects/<project>.local.yaml`) + task
override. Never hard-codes project specifics. The base branch is project
configuration (default `main`); profiles no longer carry hardcoded `model:`
lines — model ids come from `~/.config/supervisor-cao/models.local.yaml`,
produced by `scripts/detect-models`.

### Generated artifact patterns (`src/supervisor_cao/projects/config.py`)

`generated_artifact_patterns` is a project-level config field (list of
gitignore-style globs) naming paths that are *generated*, not authored —
e.g. `dist/`, `build/`, `*.min.js`, `coverage/`. Default is **empty**: no
patterns, no rejection.

When non-empty, the Executor rejects any candidate commit whose diff touches
a matching path — generated artifacts must not be committed. Matching is
path-based against the candidate's file list; there is **no unbounded
`git clean`** and no removal of untracked files. Rejected candidates are
returned to the Executor with the offending paths listed; the policy layer
never deletes files.

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

---

# 架构

Supervisor-CAO 是一个基于 [AWS Labs CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator) 构建的通用、安全优先的多 Agent 软件开发平台。

## 设计原则

**确定性代码强制执行策略，Prompt 只负责解释。**

预算、锁、SHA 校验、状态转换、同步门禁和权限全部由 Python 代码（`src/supervisor_cao/`）强制执行，从不依赖 LLM Prompt。Supervisor 可以*描述*规则，但只有策略层能*执行*规则。

## 总体流程

```
用户
  ↓
OpenCode + GLM/Qwen Supervisor
  ↓
确定性策略层（状态机、预算、SHA、锁、门禁）
  ├── Researcher        （GLM/Qwen，只读）
  ├── Codex Planner     （只读，付费，1 次调用）
  ├── GLM Executor      （可写，独立 worktree）
  ├── Qwen Verifier     （只读测试，远程验证池）
  ├── Codex Reviewer    （只读，付费，1 次调用）
  └── Codex Judge       （只读，付费，仅争议时调用）
```

标准任务生命周期：

```
Research → Codex Plan → GLM Implement → WSL2 快速验证
→ 远程验证池验证 → Qwen 报告 → Codex 完整 Review
→（如 CHANGES_REQUESTED：GLM Fix → 重新验证 → Codex 增量 Review）
→ APPROVED → Draft PR → Windows 同步 → READY_FOR_HUMAN_REVIEW
```

平台永不自动合并，始终停在 `READY_FOR_HUMAN_REVIEW`。

## 项目适配器与验证后端（`src/supervisor_cao/projects/adapter.py`）

平台核心永不硬编码项目名、基础分支、测试运行器或模型 id。一切来自
`ProjectConfig` 和两个通用接口：

- **`ProjectAdapter`**：从项目配置暴露基础分支、任务分支模板、worktree 根
  目录及仓库路径。核心向适配器询问，绝不自行假设。
- **`ValidationBackend`**：通过配置的命令/插件运行本地与远程验证
  （`run_local` / `run_remote`）。核心仅读取退出码、日志、SHA 和结构化
  结果。后端不能改变通过/失败的判定 —— 那是退出码的职责。模型仅负责总结。

`local_fixture` 是仅测试用的标记：被标记为 local fixture 的后端可在测试中
模拟验证，但生产代码绝不能把模拟结果写入 `REMOTE_VERIFIED`。`test_mode`
通过 `PolicyGateway(test_mode=True)` 依赖注入 —— 没有 `.test-mode` 文件。

## 可配置的验证命令

验证命令不硬编码到任何测试运行器、基准套件或环境管理器。
`scripts/run-verification` 以单次 try/finally 事务运行远程验证
（选择容器 → 获取 owner 锁 → 检查干净 → 记录 git 状态 → checkout 候选 →
运行命令 → 恢复 git 状态 → 释放锁），并接受：

- `--verify-command 'CMD'`（可重复）—— 在容器内运行的一条或多条 shell 命令，
- `--verify-script FILE` —— 在容器内运行的脚本文件，或
- `--setup-command 'CMD'` —— 验证前的一次性 setup 命令。

若都不传入，则使用项目配置的 `default_verification.remote` 步骤。平台核心
仅读取该脚本的退出码及其写入的结构化 `verification.json`。

## StageStore 与 candidate_sha 失效（`src/supervisor_cao/mcp/stage_store.py`）

任务的每个阶段都以 SQLite 记录，含状态、CAO `terminal_id`、artifact、当时
的 `candidate_sha` 以及 Codex 预算调用 id（如有）。恢复规则（在代码中强制）：

- **COMPLETED** 阶段且 `candidate_sha` **相同**：恢复时永不重跑 —— Worker 不
  重新启动、Codex 预算不重复花费、不产生重复 commit/PR/Windows-sync。
- **COMPLETED** 阶段但 `candidate_sha` **不同**（修复产生了新 SHA）：视为
  **过期，必须重跑** —— 重新验证与增量评审是强制性的。
- 过期的 **RUNNING** 记录（超过过期 TTL）会被回收并开始新一轮；仍在活动的
  RUNNING 记录会被复用。

Executor 在接受候选前会验证：其 SHA 与 base SHA 不同、存在真实 diff、位于
正确的任务分支、且已推送到远程。

## CAO 集成

CAO 运行本地 `cao-server`（HTTP API + Web UI，端口 9889），在隔离的 tmux 会话中启动 provider CLI（OpenCode、Codex）。Supervisor 使用 CAO 的 `assign` / `handoff` / `send_message` MCP 工具向 worker 委派任务。

- **OpenCode provider**（实验性）：多 Agent callback 使用 inbox polling fallback（CAO issues #203/#115）。长任务消息投递需单独测试。CAO 将 OpenCode 配置隔离在 `~/.aws/opencode/`，与用户个人 `~/.config/opencode/` 分离。
- **Codex CLI provider**：非交互方式（`codex exec`）用于只读的 Plan/Review/Judge。ChatGPT Pro 认证。

CAO 锁定到特定 commit（`config/cao_pinned.sha`）。升级是显式的（`supervisor-cao upgrade`），且先跑回归测试。

## 策略层组件

### 状态机（`src/supervisor_cao/state/machine.py`）

基于 SQLite 的任务状态存储。强制执行：
- 仅合法的前向转换（不跳过状态）。
- SHA 匹配：`tested_sha == candidate_sha`；`reviewed_sha == tested_sha`。
- 任何新的 `candidate_sha` 使 `tested_sha` 和 `reviewed_sha` 失效。
- 终端成功状态前的门禁检查（LOCAL_VERIFIED、REMOTE_VERIFIED、APPROVED、DRAFT_PR_CREATED 需要 SHA 一致性）。
- 错误状态（LOCAL_WORKTREE_DIRTY、CODEX_BUDGET_EXHAUSTED、NO_PROGRESS、WINDOWS_SYNC_BLOCKED 等）可从任何非终端状态到达。
- 完整审计日志（events 表）。

状态：CREATED → RESEARCHING → PLANNING → PLAN_READY → IMPLEMENTING → IMPLEMENTED → LOCAL_VERIFYING → LOCAL_VERIFIED → REMOTE_QUEUED → REMOTE_VERIFYING → REMOTE_VERIFIED → REVIEWING →（CHANGES_REQUESTED → FIXING → ... → INCREMENTAL_REVIEWING）→ APPROVED → DRAFT_PR_CREATED → WINDOWS_SYNCED → READY_FOR_HUMAN_REVIEW。加上 FAILED / NEEDS_HUMAN 终端状态。

### Codex 预算（`src/supervisor_cao/budget/codex.py`）

每任务、每角色预算。最多 4 次调用：planner(1) + full_review(1) + incremental_review(1) + judge(1)。锁下原子性消费。超限时抛出 `BudgetExhausted`（`CODEX_BUDGET_EXHAUSTED`）。持久化调用日志，含输入/输出 artifact、candidate SHA、时间戳。

### Worktree 管理（`src/supervisor_cao/workers/worktrees.py`）

每任务隔离 worktree：`~/cao-worktrees/<project>/<task-id>/{executor,verifier,reviewer}`。
- executor：可写，在 `agent/<task-id>` 分支上
- verifier/reviewer：只读 checkout
- 主 clone 仅用于 fetch/分支/worktree 管理，永不直接编辑
- 不 force push，不改写基础分支，每个有效 candidate 都 commit+push

### WorkerMonitor（`src/supervisor_cao/workers/monitor.py`）

每个 worker 进程以**双句柄模型**封装：`CaoTerminalHandle`（CAO tmux 会话 id）
加 `ProcessHandle`（OS 层 CLI 进程）。两者关联但独立追踪，因此即使 CAO 终端
记录仍声称 RUNNING，死进程也能被检出。

确定性接口：`start_worker` / `poll_worker` / `wait_for_stage` / `resume_worker`。
轮询将进程存活状态与已记录的阶段进度对比；绝不仅依赖 worker 自报状态。

卡顿检测：worker 在 `stall_timeout`（默认 `1800` 秒 = 30 分钟）内无阶段进度
则标记为 `STALLED`。`STALLED` 是**句柄状态**，不是 `TaskState` —— 底层任务
保持当前状态，可由新 worker 通过 `resume_worker` 恢复。

并发所有权：每个句柄携带 `owner_id`、`lease_until`、`heartbeat`；只有当前租约
持有者可轮询或恢复。租约在 `lease_until` 过期；新 owner 仅在过期后方可接管。

`max_runtime` 默认为 `null` —— **无总运行时超时**。监控基于进度（卡顿检测），
而非墙钟：持续推阶段进度的 worker 永不因"跑太久"被杀。

### 远程验证池（`src/supervisor_cao/validation/remote_pool.py`）

通用的远程验证池，通过 SSH 管理并使用原子锁（远程 flock/mkdir）。验证前记录原始分支/HEAD，拒绝 dirty 仓库，验证后恢复（不 `reset --hard`，不 `clean -fdx`）。恢复失败标记 UNHEALTHY。Supervisor 只读取：AVAILABLE / BUSY / UNHEALTHY / DIRTY / UNREACHABLE。真实主机名、容器名、用户名和路径均为私有（仅本地配置）；核心对池保持不透明。

### Windows 同步（`src/supervisor_cao/validation/windows_sync.py`）

仅 fast-forward 同步到 Windows 仓库。7 个门禁必须全部通过：candidate_pushed、tested_eq_candidate、reviewed_eq_candidate、review_approved、draft_pr_created、windows_clean、fast_forwardable。永不 reset --hard，永不覆盖 dirty，永不强制 checkout，永不 cherry-pick，永不 merge dev。最终检查：`Windows HEAD == candidate SHA`。

### Judge 仲裁（`src/supervisor_cao/state/judge.py`）

当 Reviewer 返回 `CHANGES_REQUESTED` 时，**所有** findings —— 不只是争议项 ——
都交给 Judge。Judge 给出四种裁决之一：

- **`OVERTURN`** —— 所有 Reviewer findings 被推翻 → 任务直接进入 `APPROVED`。
- **`UPHOLD`** —— 所有 findings 被维持 → 回到 `FIXING`。
- **`MIXED`** —— 部分推翻、部分维持 → 仅对维持项回到 `FIXING`。
- **`UNRESOLVED`** —— Judge 无法判定 → `NEEDS_HUMAN`。

晋升规则：**只有全部 `OVERTURN` 的裁决才晋升到 `APPROVED`**。
`UPHOLD`、`MIXED`、`UNRESOLVED` 都不导向 `APPROVED` —— `UPHOLD`/`MIXED` 去
`FIXING`，`UNRESOLVED` 去 `NEEDS_HUMAN`。Judge 永不自动合并，只对 Reviewer 的
findings 做仲裁。

### 项目配置（`src/supervisor_cao/projects/config.py`）

分层加载：公开示例（`config/examples/<project>.example.yaml`）+ 私有本地（`~/.config/supervisor-cao/projects/<project>.local.yaml`）+ 任务级覆盖。永不硬编码项目特定逻辑。基础分支属于项目配置（默认 `main`）；profiles 不再带有硬编码的 `model:` 行 —— 模型 id 来自 `~/.config/supervisor-cao/models.local.yaml`，由 `scripts/detect-models` 生成。

### 生成物 pattern（`src/supervisor_cao/projects/config.py`）

`generated_artifact_patterns` 是项目级配置字段（gitignore 风格 glob 列表），
命名*生成*而非*编写*的路径，例如 `dist/`、`build/`、`*.min.js`、`coverage/`。
默认为**空**：无 pattern、无拒绝。

非空时，Executor 拒绝任何 diff 触碰匹配路径的 candidate commit —— 生成物不得
提交。匹配基于 candidate 文件列表的路径；**不做无界 `git clean`**，也不删除
未跟踪文件。被拒 candidate 连同违规路径列表退回 Executor；策略层永不删除文件。

## 角色权限

| 角色 | 源码访问 | Git | Codex 预算 |
|------|---------|-----|-----------|
| Supervisor | 读取任务状态 + artifact | 无（仅编排） | 无 |
| Researcher | 只读 | 只读 | 无 |
| Codex Planner | 只读 | 只读 | 1 planner |
| GLM Executor | 仅自己的 worktree | commit+push 任务分支 | 无 |
| Qwen Verifier | 只读 + 脚本 | 无 | 无 |
| Codex Reviewer | 只读 | 只读 | 1 full_review（+1 incremental） |
| Codex Judge | 只读 | 只读 | 1 judge（仅争议） |

## 公开 vs 本地数据

公开仓库：通用代码、profiles（无密钥）、schemas、脱敏示例、测试、文档、CAO 锁定 SHA。

仅本地（`~/.config/supervisor-cao/`、`~/.local/state/supervisor-cao/`、`~/cao-runs/`）：API key、SSH 主机、容器名、用户名、路径、运行日志、models.local.yaml、*.local.yaml、secrets.env。

每次 push 运行 `scripts/scan-secrets` 阻止泄露。
