[English](#adding-a-project) | [简体中文](#添加项目)

# Adding a Project

A project integration adds **configuration and (rarely) validation adapters**.
The platform stays generic: state machine, budgets, SHA checks, locks, review
gates, and sync safety are project-independent.

## Principles

1. **Public repo stays sanitized** — no real hosts, container names, usernames,
   or private paths in tracked files.
2. **Private data lives in `~/.config/supervisor-cao/`** — layered local config
   fills in real values at runtime.
3. **No project-specific code in the policy layer** — if generic command
   execution is insufficient, add a thin validation adapter, never a fork of
   the state machine or budget logic.

## Step 1 — Sanitized example config (committed)

Add `config/examples/<project>.example.yaml` with placeholders. Abbreviated
from `config/examples/pandas.example.yaml`:

```yaml
name: pandas
base_branch: dev
task_branch_prefix: agent/
wsl_repo: "~/projects/pandas"                 # placeholder; real value is private
windows_repo: "<WINDOWS_REPO_PATH>"           # placeholder
default_verification:
  wsl_quick:   { build: true, focused_pytest: true, candidate_sha_check: true }
  remote_pool: { editable_install: true, correctness_tests: true, asv: true, log_collection: true }
  report:      { structured: true }
remote_validation:
  ssh_host: "<SSH_HOST>"
  containers: ["<CONTAINER_A>", "<CONTAINER_B>"]
  user: "<REMOTE_USER>"
  repo_path: "<REMOTE_REPOSITORY_PATH>"
  conda_env: "<CONDA_ENVIRONMENT>"
executor_limits:
  max_rounds: 8
  max_no_progress_rounds: 2
  push_every_valid_candidate: true
  require_clean_worktree: true
  require_commit: true
codex_budget:
  max_calls_per_task: 4
  planner: 1
  full_review: 1
  incremental_review: 1
  judge: 1
```

## Step 2 — Private local config (git-ignored)

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/pandas.example.yaml \
   ~/.config/supervisor-cao/projects/pandas.local.yaml
```

Fill in real values: `wsl_repo`, `windows_repo`, and `remote_validation`
(`ssh_host` alias from `~/.ssh/config`, real container names, user, repo path,
conda env). Never commit this file.

## Step 3 — (Optional) Validation adapter

Only if generic command execution is insufficient, add a thin adapter under
`src/supervisor_cao/validation/`. Prefer declaring commands and selectors in
YAML over writing project-specific Python. The policy layer (state, budget,
SHA, locks, sync gates) must remain untouched.

## Step 4 — Tests and fixtures

- Unit tests for any new config schema fields.
- E2E fixture using a temporary repository (never the real project repo).
- Confirm the real-project smoke test is **read-only**: loads config, checks
  the base branch is reachable, inspects local repos, health-checks remote
  slots — without modifying anything.

## Config schema reference

| Field | Purpose |
|-------|---------|
| `name` | Project identifier (matches `supervisor-cao chat <name>`). |
| `base_branch` | Base branch task branches are cut from (e.g. `dev`). Never rewritten. |
| `task_branch_prefix` | Prefix for task branches (default `agent/`). |
| `wsl_repo` | WSL Linux path to the agent's clone. |
| `windows_repo` | Windows repo path the sync script fast-forwards (from local config). |
| `remote_validation` | SSH host, containers, user, repo path, conda env for the remote pool. |
| `default_verification` | WSL quick checks, remote pool checks, reporting toggles. |
| `executor_limits` | `max_rounds`, `max_no_progress_rounds`, push/clean/commit requirements. |
| `codex_budget` | Per-task Codex call caps per role (enforced in code). |

## Layering and task overrides

Config loads in order, later wins: (1) `config/examples/<project>.example.yaml`
(public), (2) `~/.config/supervisor-cao/projects/<project>.local.yaml`
(private), (3) task-level overrides from the task file. Task files may tighten
verification but must supply the performance quartet (`baseline_sha`,
`benchmark_selector`, `performance_acceptance`, `required_test_scope` plus
`regression_threshold`); missing critical params route to `NEEDS_HUMAN`.

## Checklist

- [ ] Sanitized example committed under `config/examples/`.
- [ ] Private local config created and git-ignored.
- [ ] `supervisor-cao doctor` lists the project.
- [ ] Read-only smoke test passes (nothing modified).
- [ ] `scripts/scan-secrets` passes on the example file.
- [ ] No project-specific code in the policy layer.

## See also

`docs/USER_GUIDE.md`, `docs/SECURITY.md`, `docs/ACCEPTANCE.md`.

---

# 添加项目

项目集成会添加**配置以及（极少情况下的）验证适配器**。
平台保持通用：状态机、预算、SHA 检查、锁、评审门禁以及同步安全机制均与具体项目无关。

## 原则

1. **公开仓库保持脱敏** — 受跟踪的文件中不包含真实主机名、容器名、用户名或私有路径。
2. **私有数据存放在 `~/.config/supervisor-cao/`** — 分层本地配置在运行时填入真实值。
3. **策略层中不得有项目特定代码** — 如果通用的命令执行不够用，请添加一个轻量的验证适配器，绝不fork状态机或预算逻辑。

## 步骤 1 — 脱敏的示例配置（已提交）

添加 `config/examples/<project>.example.yaml`，使用占位符。以下为 `config/examples/pandas.example.yaml` 的简略版本：

```yaml
name: pandas
base_branch: dev
task_branch_prefix: agent/
wsl_repo: "~/projects/pandas"                 # 占位符；真实值为私有
windows_repo: "<WINDOWS_REPO_PATH>"           # 占位符
default_verification:
  wsl_quick:   { build: true, focused_pytest: true, candidate_sha_check: true }
  remote_pool: { editable_install: true, correctness_tests: true, asv: true, log_collection: true }
  report:      { structured: true }
remote_validation:
  ssh_host: "<SSH_HOST>"
  containers: ["<CONTAINER_A>", "<CONTAINER_B>"]
  user: "<REMOTE_USER>"
  repo_path: "<REMOTE_REPOSITORY_PATH>"
  conda_env: "<CONDA_ENVIRONMENT>"
executor_limits:
  max_rounds: 8
  max_no_progress_rounds: 2
  push_every_valid_candidate: true
  require_clean_worktree: true
  require_commit: true
codex_budget:
  max_calls_per_task: 4
  planner: 1
  full_review: 1
  incremental_review: 1
  judge: 1
```

## 步骤 2 — 私有本地配置（已 git-ignored）

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/pandas.example.yaml \
   ~/.config/supervisor-cao/projects/pandas.local.yaml
```

填入真实值：`wsl_repo`、`windows_repo` 以及 `remote_validation`
（`ssh_host` 别名来自 `~/.ssh/config`，真实容器名、用户、仓库路径、conda 环境）。绝不要提交此文件。

## 步骤 3 —（可选）验证适配器

仅当通用命令执行不够用时，才在 `src/supervisor_cao/validation/` 下添加一个轻量适配器。优先在 YAML 中声明命令和选择器，而非编写项目特定的 Python 代码。策略层（状态、预算、SHA、锁、同步门禁）必须保持不变。

## 步骤 4 — 测试与 fixtures

- 为任何新配置 schema 字段编写单元测试。
- 使用临时仓库（绝不使用真实项目仓库）的 E2E fixture。
- 确认真实项目的 smoke test 是**只读的**：加载配置、检查 base 分支可达、检查本地仓库、对远程槽位做健康检查 — 不修改任何内容。

## 配置 schema 参考

| 字段 | 用途 |
|-------|---------|
| `name` | 项目标识符（与 `supervisor-cao chat <name>` 匹配）。 |
| `base_branch` | 任务分支由此 base 分支切出（例如 `dev`）。永不被重写。 |
| `task_branch_prefix` | 任务分支前缀（默认为 `agent/`）。 |
| `wsl_repo` | agent 克隆仓库的 WSL Linux 路径。 |
| `windows_repo` | 同步脚本执行快进的 Windows 仓库路径（来自本地配置）。 |
| `remote_validation` | 远程池的 SSH 主机、容器、用户、仓库路径、conda 环境。 |
| `default_verification` | WSL 快速检查、远程池检查、报告开关。 |
| `executor_limits` | `max_rounds`、`max_no_progress_rounds`、push/clean/commit 要求。 |
| `codex_budget` | 每个任务每个角色的 Codex 调用上限（在代码中强制执行）。 |

## 分层与任务覆盖

配置按以下顺序加载，后者覆盖前者：(1) `config/examples/<project>.example.yaml`（公开），(2) `~/.config/supervisor-cao/projects/<project>.local.yaml`（私有），(3) 来自任务文件的任务级覆盖。任务文件可以收紧验证，但必须提供性能四元组（`baseline_sha`、`benchmark_selector`、`performance_acceptance`、`required_test_scope` 外加 `regression_threshold`）；缺失关键参数会路由到 `NEEDS_HUMAN`。

## 检查清单

- [ ] 脱敏的示例已提交至 `config/examples/`。
- [ ] 私有本地配置已创建并被 git-ignored。
- [ ] `supervisor-cao doctor` 能列出该项目。
- [ ] 只读 smoke test 通过（无任何修改）。
- [ ] `scripts/scan-secrets` 在示例文件上通过。
- [ ] 策略层中无项目特定代码。

## 另请参阅

`docs/USER_GUIDE.md`、`docs/SECURITY.md`、`docs/ACCEPTANCE.md`。
