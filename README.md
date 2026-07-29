# Supervisor-CAO

[English](#supervisor-cao) | [简体中文](#supervisor-cao-简体中文)

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

---

# Supervisor-CAO 简体中文

一个基于 [AWS Labs CLI Agent Orchestrator (CAO)](https://github.com/awslabs/cli-agent-orchestrator) 构建的通用、安全优先的多 Agent 软件开发平台。

Supervisor-CAO 协调多个 AI 编码 CLI，让低成本的 GLM/Qwen Supervisor 将工作委派给专门的 Agent，只在**高价值任务**中调用 Codex（制定计划、完整审查、增量审查、争议仲裁）。确定性代码——而非 Prompt——强制执行预算、SHA 校验、锁、状态转换和同步门禁。

> **ZCode + GLM 5.2 仅负责搭建本平台，不接入运行时 Agent 团队。**

## 架构

```
用户
  ↓
OpenCode + GLM/Qwen Supervisor
  ↓
确定性策略层（状态机、预算、SHA、锁、同步门禁）
  ├── Researcher        （GLM/Qwen，只读）
  ├── Codex Planner     （只读，付费）
  ├── GLM Executor      （可写，独立 worktree）
  ├── Qwen Verifier     （只读测试，远程验证池）
  ├── Codex Reviewer    （只读，付费）
  └── Codex Judge       （只读，付费，仅争议时调用）
```

## 成本策略

- **GLM/Qwen**：常驻调度、高频实现、测试、诊断、报告压缩。
- **Codex**：低频 Plan、完整 Review、增量 Review、争议仲裁。每任务最多 4 次调用，由代码强制执行。
- **确定性代码**：状态机、预算、SHA 校验、锁、同步、安全门禁。

## 状态

平台最终停在 `READY_FOR_HUMAN_REVIEW`。不自动合并。不修改基础分支。不 force push。

### 验证结果

| 测试 | 结果 |
|------|------|
| 单元测试（状态机、预算、Schema、SHA、锁、Windows 同步、配置、secret scan） | 51 通过 |
| 集成测试（Planner、Executor 修复、Verifier 失败、stale、预算、池、Windows 阻塞、happy path） | 10 通过 |
| E2E（临时仓库完整流程：状态+预算+worktree+commit+push+验证+review+Draft PR+Windows 同步） | 13/13 通过 |
| 稳定性（连续 10 次 E2E） | 10/10 通过 |
| Supervisor 模型测试（GLM 5.2 + Qwen 3.7 Max） | 均 4/4，Qwen 为主（更快） |
| pandas 只读 smoke | 8 PASS（配置、origin/dev、Windows 仓库 dirty 检测、SSH、2 容器、conda+pandas、池锁） |
| `supervisor-cao doctor` | CAO/OpenCode/Codex/uv/tmux/projects/pinned-SHA 全部 ✓ |
| secret scan（push 前） | 干净，无私有文件被跟踪 |

### 已知限制

- **WSL2 网络受限**：iKuuuVPN 系统代理模式下网络可用，但 TUN 模式会劫持 DNS（fake-ip）。CAO 已通过离线 wheelhouse 安装（`uv tool install --offline --find-links`）。`supervisor-cao upgrade` 需网络或刷新 wheelhouse。
- **CAO OpenCode provider 实验性**：多 Agent callback 使用 inbox polling fallback（CAO issues #203/#115）。长任务消息回传和恢复需真实 CAO 多 Agent 实测后才能无人值守运行。
- **Supervisor benchmark 是能力探测**：完整 CAO `handoff`/`assign`/`send_message` 多 Agent 测试需要活的 `cao-server` + worker 会话，不仅是 `opencode run`。

## 快速开始

```bash
# 1. 确保 WSL2 Ubuntu-24.04 已安装 CAO、OpenCode、Codex CLI
# 2. 启动平台
supervisor-cao up

# 3. 诊断环境
supervisor-cao doctor

# 4. 进入项目的 Supervisor
supervisor-cao chat pandas

# 5. 状态 / 任务
supervisor-cao status
supervisor-cao task list

# 6. 停止
supervisor-cao down
```

完整安装见 `docs/INSTALL.md`，使用见 `docs/USER_GUIDE.md`。

## 仓库结构

```
Supervisor-CAO/
├── bin/supervisor-cao          # 启动器
├── profiles/                   # Agent Profiles（无密钥）
├── config/                     # 脱敏示例 + CAO 锁定 SHA
├── schemas/                    # Agent artifact 的 JSON Schema
├── scripts/                    # detect-models、manage-worktrees、remote-pool、...
├── src/supervisor_cao/         # 确定性策略层
│   ├── state/                  # 任务状态机 + SQLite 存储
│   ├── budget/                 # Codex 调用预算
│   ├── projects/               # 项目配置加载
│   ├── workers/                # worktree 管理
│   ├── validation/             # 远程池 + Windows 同步
│   └── cli/                    # supervisor-cao CLI
├── tests/                      # 单元、集成、E2E
└── docs/                       # INSTALL、USER_GUIDE、SECURITY、...
```

## 私有数据隔离

机器相关数据（API Key、SSH 主机、容器名、用户名、路径）存放在 `~/.config/supervisor-cao/`，永不提交。详见 `docs/SECURITY.md`。

## CAO 版本

CAO 锁定到特定 commit（见 `config/cao_pinned.sha`）。升级是显式的（`supervisor-cao upgrade`），且先跑回归测试。

## 许可证

Apache-2.0
