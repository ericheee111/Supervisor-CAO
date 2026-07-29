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
→ 920B 远程验证 → Qwen 报告 → Codex 完整 Review
→（如 CHANGES_REQUESTED：GLM Fix → 重新验证 → Codex 增量 Review）
→ APPROVED → Draft PR → Windows 同步 → READY_FOR_HUMAN_REVIEW
```

平台永不自动合并，始终停在 `READY_FOR_HUMAN_REVIEW`。

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

### 远程验证池（`src/supervisor_cao/validation/remote_pool.py`）

通过 SSH 管理的 920B 双容器池。原子锁（远程 flock/mkdir）。验证前记录原始分支/HEAD，拒绝 dirty 仓库，验证后恢复（不 `reset --hard`，不 `clean -fdx`）。恢复失败标记 UNHEALTHY。Supervisor 只读取：AVAILABLE / BUSY / UNHEALTHY / DIRTY / UNREACHABLE。

### Windows 同步（`src/supervisor_cao/validation/windows_sync.py`）

仅 fast-forward 同步到 Windows 仓库。7 个门禁必须全部通过：candidate_pushed、tested_eq_candidate、reviewed_eq_candidate、review_approved、draft_pr_created、windows_clean、fast_forwardable。永不 reset --hard，永不覆盖 dirty，永不强制 checkout，永不 cherry-pick，永不 merge dev。最终检查：`Windows HEAD == candidate SHA`。

### 项目配置（`src/supervisor_cao/projects/config.py`）

分层加载：公开示例（`config/examples/<project>.example.yaml`）+ 私有本地（`~/.config/supervisor-cao/projects/<project>.local.yaml`）+ 任务级覆盖。永不硬编码项目特定逻辑。

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
