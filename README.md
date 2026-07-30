# Supervisor-CAO

[English](#overview) | [简体中文](#概述)

## Overview

**Supervisor-CAO** is a generic, safety-first multi-agent software-development
platform built on [AWS Labs CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator).

It lets a **cheap LLM Supervisor** (GLM / Qwen via OpenCode) coordinate
specialist agents to research, plan, implement, verify, and review code — while
reserving the **expensive Codex** for only the highest-value steps (planning,
full review, incremental review, dispute arbitration).

The key idea: **deterministic code enforces policy, prompts only explain it.**
Budgets, SHA matching, state transitions, remote locks, and sync gates are all
enforced in Python — not left to an LLM's self-discipline.

### Why?

- **Cost control**: 90%+ of work runs on cheap models; Codex is capped at 4
  calls per task.
- **Safety**: no auto-merge, no force-push, no base-branch rewrite, no
  overwriting dirty worktrees. The platform always stops at
  `READY_FOR_HUMAN_REVIEW`.
- **Generic by design**: the platform has zero project-specific code. A fully
  fictional `demo-project` example ships in `config/examples/`. Add a new
  project = add a config file + optional validation rules.
- **Auditable**: every state transition, Codex call, SHA, and lock is persisted
  to SQLite with a full event log.

## How it works

```
You describe a task
        │
        ▼
┌─────────────────────────────────────────────┐
│  GLM/Qwen Supervisor (OpenCode)             │
│  ┌───────────────────────────────────────┐  │
│  │  Deterministic policy layer (code)    │  │
│  │  state machine · budget · SHA · locks │  │
│  └───────────────────────────────────────┘  │
└───────┬─────────────────────────────────────┘
        │
        ├── Researcher        (GLM/Qwen, read-only, free)
        ├── Codex Planner     (read-only, 1 call)
        ├── GLM Executor      (writes own worktree, builds + tests + commits)
        ├── Qwen Verifier     (read-only, runs remote validation pool)
        ├── Codex Reviewer    (read-only, 1 call)
        └── Codex Judge       (read-only, only on real disputes)
        │
        ▼
  Draft PR + Windows sync ──► READY_FOR_HUMAN_REVIEW
  (never auto-merges)
```

**Standard task flow:**

```
Research → Codex Plan → GLM Implement → WSL2 quick verify
→ Remote pool verify → Qwen report → Codex full Review
→ (if changes requested: GLM Fix → re-verify → Codex incremental Review)
→ APPROVED → Draft PR → Windows sync → READY_FOR_HUMAN_REVIEW
```

## Quick start

### Prerequisites

- **WSL2 Ubuntu-24.04** (or any Linux with Python 3.10+, tmux 3.3+)
- [uv](https://docs.astral.sh/uv/) installed
- At least one provider CLI authenticated:
  - [OpenCode CLI](https://opencode.ai) (for GLM/Qwen)
  - [Codex CLI](https://github.com/openai/codex) (for Codex, ChatGPT auth)
- [CAO](https://github.com/awslabs/cli-agent-orchestrator) installed:
  ```bash
  uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main
  cao init
  ```

### Install Supervisor-CAO

```bash
git clone https://github.com/ericheee111/Supervisor-CAO.git
cd Supervisor-CAO
pip install -e .              # installs the supervisor-cao CLI
```

### Configure

```bash
# 1. Detect your OpenCode providers/models (writes ~/.config/supervisor-cao/models.local.yaml)
python scripts/detect-models

# 2. Create a project config (copy the sanitized example)
cp config/examples/demo-project.example.yaml ~/.config/supervisor-cao/projects/myproject.local.yaml
# edit myproject.local.yaml with your real paths, SSH host, containers, etc.
```

### Run

```bash
supervisor-cao up              # start cao-server (Web UI at http://localhost:9889)
supervisor-cao doctor          # verify environment
supervisor-cao chat myproject  # enter interactive Supervisor
```

That's it. Describe your task to the Supervisor and it coordinates the agents.

## Demo

A self-contained end-to-end demo that runs the full policy layer on a temporary
git repo (no real LLM calls — worker results are mocked as artifacts):

```bash
python tests/e2e/test_temp_repo_e2e.py
```

Sample output:

```
  ✓ planner budget consumed: 1/1
  ✓ executor commit+push: sha=f5f0defc8af1
  ✓ verified, tested==candidate: tested=f5f0defc8af1
  ✓ reviewer budget consumed: total 2/4
  ✓ draft PR body has READY_FOR_HUMAN_REVIEW: generated
  ✓ windows sync blocked when dirty: dirty detected
  ✓ windows gates pass when clean+pushed: clean=True pushed=True ff=True
  ✓ windows sync completed: head=f5f0defc8af1
  ✓ reached READY_FOR_HUMAN_REVIEW: done
  ✓ codex budget 2/4 used: 2/4
  ✓ SHA integrity: tested==reviewed==candidate: all=f5f0defc8af1

E2E Summary: 13 PASS, 0 FAIL
```

This demonstrates: state machine transitions, Codex budget enforcement, worktree
isolation, commit+push, SHA binding, Windows sync gates (blocked when dirty,
passes when clean), Draft PR generation, and budget accounting — all enforced by
deterministic code.

## CLI reference

```bash
supervisor-cao up                   # start cao-server
supervisor-cao down                 # stop all CAO sessions
supervisor-cao doctor               # diagnose environment
supervisor-cao chat <project>       # interactive Supervisor for a project
supervisor-cao run <project> \
    --task-file task.md             # non-interactive task run
supervisor-cao status               # platform status
supervisor-cao task list            # list tasks
supervisor-cao task show <task-id>  # task details + event log
supervisor-cao task logs <task-id>  # task artifacts
supervisor-cao upgrade              # upgrade CAO (runs regression first)
```

## Adding a new project

The platform is generic. To add a project:

1. Create `config/examples/<project>.example.yaml` (sanitized, public):
   ```yaml
   name: myproject
   base_branch: main
   task_branch_prefix: agent/
   wsl_repo: "~/projects/myproject"
   executor_limits:
     max_rounds: 8
     max_no_progress_rounds: 2
   codex_budget:
     max_calls_per_task: 4
   ```

2. Create `~/.config/supervisor-cao/projects/<project>.local.yaml` (private:
   real SSH hosts, container names, paths — never committed).

3. (Optional) Add project-specific validation rules or benchmark selectors in
   the task file.

See `docs/ADD_PROJECT.md` for the full guide.

## Repository layout

```
Supervisor-CAO/
├── bin/supervisor-cao            # launcher
├── profiles/                     # 7 agent profiles (no secrets)
│   ├── supervisor.md             # GLM/Qwen Supervisor
│   ├── researcher.md             # read-only research
│   ├── codex-planner.md          # Codex, read-only, 1 call
│   ├── glm-executor.md           # writable, own worktree
│   ├── qwen-verifier.md          # read-only, remote pool
│   ├── codex-reviewer.md         # Codex, read-only, 1 call
│   └── codex-judge.md            # Codex, disputes only
├── schemas/                      # JSON schemas for agent artifacts
├── config/
│   ├── examples/                 # sanitized project config examples
│   └── cao_pinned.sha            # pinned CAO commit
├── scripts/                      # detect-models, manage-worktrees, remote-pool,
│                                 # run-verification, create-draft-pr,
│                                 # sync-windows-repo, scan-secrets
├── src/supervisor_cao/           # deterministic policy layer
│   ├── state/                    # task state machine (SQLite)
│   ├── budget/                   # Codex call budget (4/task, code-enforced)
│   ├── projects/                 # project config loader
│   ├── workers/                  # worktree lifecycle
│   ├── validation/               # remote pool locks + Windows sync gates
│   └── cli/                      # supervisor-cao CLI
├── tests/                        # unit, integration, e2e
└── docs/                         # INSTALL, USER_GUIDE, SECURITY, ...
```

## Key design decisions

- **No LLM owns policy.** Budgets, locks, SHA matching, state transitions, and
  sync gates are in Python code. Prompts explain the rules; they don't enforce
  them.
- **SHA binding.** Verification and review results are valid only for their
  exact commit SHA. Any new commit invalidates prior results. `tested_sha` must
  equal `candidate_sha`; `reviewed_sha` must equal `tested_sha`.
- **Codex budget.** Max 4 calls per task: Planner(1) + Full Review(1) +
  Incremental Review(1) + Judge(1). Enforced atomically in SQLite. On
  exhaustion: `CODEX_BUDGET_EXHAUSTED` → task stops, human required.
- **Remote pool safety.** Atomic lock per container, record/restore original
  git state, refuse dirty repos, never `reset --hard` or `clean -fdx`. Mark
  `UNHEALTHY` on restore failure.
- **Windows sync gates.** 7 gates must all pass (candidate pushed, SHA match,
  review approved, draft PR created, worktree clean, fast-forwardable) before
  touching the Windows repo. Fast-forward only.

## Private data isolation

All machine-specific data stays outside Git:

```
~/.config/supervisor-cao/          # models.local.yaml, projects/*.local.yaml
~/.local/state/supervisor-cao/     # task DB, codex budget DB
~/cao-runs/                        # logs, test results, audit records
```

`scripts/scan-secrets` runs before every push to block leaks (API keys, internal
hosts, container names, usernames, paths).

## Documentation

- [INSTALL.md](docs/INSTALL.md) — full installation guide
- [USER_GUIDE.md](docs/USER_GUIDE.md) — usage and workflow
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — architecture deep-dive
- [ADD_PROJECT.md](docs/ADD_PROJECT.md) — adding a new project
- [SECURITY.md](docs/SECURITY.md) — security model
- [ACCEPTANCE.md](docs/ACCEPTANCE.md) — acceptance criteria
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — common issues

## License

Apache-2.0

---

## 概述

**Supervisor-CAO** 是一个基于 [AWS Labs CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator) 构建的通用、安全优先的多 Agent 软件开发平台。

它让**低成本的 LLM Supervisor**（通过 OpenCode 调用 GLM / Qwen）协调专门的 Agent 来研究、规划、实现、验证和审查代码——同时只在**最高价值的步骤**调用 Codex（制定计划、完整审查、增量审查、争议仲裁）。

核心理念：**确定性代码强制执行策略，Prompt 只负责解释。** 预算、SHA 校验、状态转换、远程锁和同步门禁全部由 Python 代码执行——不依赖 LLM 的自律。

### 为什么用？

- **成本控制**：90%+ 的工作跑在便宜模型上；Codex 每任务最多 4 次调用。
- **安全**：不自动合并、不 force-push、不改写基础分支、不覆盖未提交改动。平台始终停在 `READY_FOR_HUMAN_REVIEW`。
- **通用设计**：平台没有任何项目特定代码，`config/examples/` 下提供完全虚构的 `demo-project` 示例。新增项目 = 加一个配置文件 + 可选验证规则。
- **可审计**：每次状态转换、Codex 调用、SHA、锁都持久化到 SQLite，含完整事件日志。

## 工作原理

```
你描述一个任务
        │
        ▼
┌─────────────────────────────────────────────┐
│  GLM/Qwen Supervisor (OpenCode)             │
│  ┌───────────────────────────────────────┐  │
│  │  确定性策略层（代码）                   │  │
│  │  状态机 · 预算 · SHA · 锁              │  │
│  └───────────────────────────────────────┘  │
└───────┬─────────────────────────────────────┘
        │
        ├── Researcher        （GLM/Qwen，只读，免费）
        ├── Codex Planner     （只读，1 次调用）
        ├── GLM Executor      （可写自己的 worktree，构建+测试+提交）
        ├── Qwen Verifier     （只读，运行远程验证池）
        ├── Codex Reviewer    （只读，1 次调用）
        └── Codex Judge       （只读，仅真实争议时调用）
        │
        ▼
  Draft PR + Windows 同步 ──► READY_FOR_HUMAN_REVIEW
  （永不自动合并）
```

## 快速开始

### 前置条件

- **WSL2 Ubuntu-24.04**（或任何有 Python 3.10+、tmux 3.3+ 的 Linux）
- [uv](https://docs.astral.sh/uv/) 已安装
- 至少一个 provider CLI 已认证：
  - [OpenCode CLI](https://opencode.ai)（用于 GLM/Qwen）
  - [Codex CLI](https://github.com/openai/codex)（用于 Codex，ChatGPT 登录）
- [CAO](https://github.com/awslabs/cli-agent-orchestrator) 已安装：
  ```bash
  uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main
  cao init
  ```

### 安装 Supervisor-CAO

```bash
git clone https://github.com/ericheee111/Supervisor-CAO.git
cd Supervisor-CAO
pip install -e .              # 安装 supervisor-cao CLI
```

### 配置

```bash
# 1. 检测 OpenCode 的 provider/模型（写入 ~/.config/supervisor-cao/models.local.yaml）
python scripts/detect-models

# 2. 创建项目配置（复制脱敏示例）
cp config/examples/demo-project.example.yaml ~/.config/supervisor-cao/projects/myproject.local.yaml
# 编辑 myproject.local.yaml，填入真实路径、SSH 主机、容器等
```

### 运行

```bash
supervisor-cao up              # 启动 cao-server（Web UI 在 http://localhost:9889）
supervisor-cao doctor          # 验证环境
supervisor-cao chat myproject  # 进入交互式 Supervisor
```

向 Supervisor 描述你的任务，它会自动协调各个 Agent。

## Demo

一个自包含的端到端 demo，在临时 git 仓库上运行完整策略层（无真实 LLM 调用——worker 结果以 artifact 模拟）：

```bash
python tests/e2e/test_temp_repo_e2e.py
```

示例输出：

```
  ✓ planner budget consumed: 1/1
  ✓ executor commit+push: sha=f5f0defc8af1
  ✓ verified, tested==candidate: tested=f5f0defc8af1
  ✓ reviewer budget consumed: total 2/4
  ✓ draft PR body has READY_FOR_HUMAN_REVIEW: generated
  ✓ windows sync blocked when dirty: dirty detected
  ✓ windows gates pass when clean+pushed: clean=True pushed=True ff=True
  ✓ windows sync completed: head=f5f0defc8af1
  ✓ reached READY_FOR_HUMAN_REVIEW: done
  ✓ codex budget 2/4 used: 2/4
  ✓ SHA integrity: tested==reviewed==candidate: all=f5f0defc8af1

E2E Summary: 13 PASS, 0 FAIL
```

展示了：状态机转换、Codex 预算强制、worktree 隔离、commit+push、SHA 绑定、Windows 同步门禁（dirty 时阻塞，clean 时通过）、Draft PR 生成、预算记账——全部由确定性代码强制执行。

## CLI 命令

```bash
supervisor-cao up                   # 启动 cao-server
supervisor-cao down                 # 停止所有 CAO 会话
supervisor-cao doctor               # 诊断环境
supervisor-cao chat <project>       # 交互式 Supervisor
supervisor-cao run <project> \
    --task-file task.md             # 非交互式任务运行
supervisor-cao status               # 平台状态
supervisor-cao task list            # 任务列表
supervisor-cao task show <task-id>  # 任务详情 + 事件日志
supervisor-cao task logs <task-id>  # 任务 artifact
supervisor-cao upgrade              # 升级 CAO（先跑回归测试）
```

## 新增项目

平台是通用的。新增项目步骤：

1. 创建 `config/examples/<project>.example.yaml`（脱敏，公开）：
   ```yaml
   name: myproject
   base_branch: main
   task_branch_prefix: agent/
   wsl_repo: "~/projects/myproject"
   executor_limits:
     max_rounds: 8
     max_no_progress_rounds: 2
   codex_budget:
     max_calls_per_task: 4
   ```

2. 创建 `~/.config/supervisor-cao/projects/<project>.local.yaml`（私有：真实 SSH 主机、容器名、路径——永不提交）。

3.（可选）在任务文件中添加项目特定的验证规则或 benchmark selector。

详见 `docs/ADD_PROJECT.md`。

## 关键设计

- **LLM 不拥有策略。** 预算、锁、SHA 校验、状态转换、同步门禁都在 Python 代码里。Prompt 解释规则，不执行规则。
- **SHA 绑定。** 验证和审查结果只对其精确的 commit SHA 有效。任何新 commit 使旧结果失效。`tested_sha` 必须等于 `candidate_sha`；`reviewed_sha` 必须等于 `tested_sha`。
- **Codex 预算。** 每任务最多 4 次：Planner(1) + Full Review(1) + Incremental Review(1) + Judge(1)。原子性 SQLite 强制。耗尽时 `CODEX_BUDGET_EXHAUSTED` → 任务停止，需人工处理。
- **远程池安全。** 每容器原子锁，记录/恢复原始 git 状态，拒绝 dirty 仓库，永不 `reset --hard` 或 `clean -fdx`。恢复失败标记 `UNHEALTHY`。
- **Windows 同步门禁。** 7 个门禁全部通过（candidate 已 push、SHA 匹配、review approved、draft PR 已创建、worktree clean、可 fast-forward）才操作 Windows 仓库。仅 fast-forward。

## 私有数据隔离

所有机器相关数据留在 Git 之外：

```
~/.config/supervisor-cao/          # models.local.yaml, projects/*.local.yaml
~/.local/state/supervisor-cao/     # 任务 DB, codex 预算 DB
~/cao-runs/                        # 日志、测试结果、审计记录
```

`scripts/scan-secrets` 在每次 push 前运行，阻止泄露（API key、内部主机、容器名、用户名、路径）。

## 文档

- [INSTALL.md](docs/INSTALL.md) — 完整安装指南
- [USER_GUIDE.md](docs/USER_GUIDE.md) — 使用和工作流
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — 架构详解
- [ADD_PROJECT.md](docs/ADD_PROJECT.md) — 新增项目
- [SECURITY.md](docs/SECURITY.md) — 安全模型
- [ACCEPTANCE.md](docs/ACCEPTANCE.md) — 验收标准
- [TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — 常见问题

## 许可证

Apache-2.0
