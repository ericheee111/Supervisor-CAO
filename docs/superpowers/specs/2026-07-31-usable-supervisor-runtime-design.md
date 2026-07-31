# Usable Supervisor Runtime 设计文档

**日期**: 2026-07-31
**分支**: `feat/usable-supervisor-runtime`
**状态**: 设计待批准
**基线**: `main` (commit `ee09ac7`)

## 1. 背景与目标

Supervisor-CAO 的运行时"骨架"（状态机、StageStore 幂等、CaoClient、WorkerRunner、
WorkerMonitor、PolicyGateway）已全部存在且测试覆盖良好，但**执行路径存在断点**：
`WorkerRunner` 各 `run_*` 方法同步直调 `CaoClient.launch_worker`，完全绕过了已实现
但未接入的 `WorkerMonitor`（`start_worker`/`wait_for_stage`/`poll_worker` 是死代码），
导致 progress-based 无超时监控、STALLED 重新挂载、崩溃后 worker 续跑等能力均不可用。
同时 CLI 缺少 `task start/watch/resume` 等长任务命令。

**本轮目标**：让 Supervisor 真正可用。用户只需 5 个 CLI 命令即可驱动完整任务闭环。
不涉及 PR handoff、Windows 同步或 forge API——任务在 Reviewer 批准后即视为完成
（`APPROVED` 为成功终态）。

**非目标**: 不做 PR 创建/内容包/Windows 同步/forge API；不合并或继续修改
`feat/pr-content-handoff`。

## 2. 现状映射（调研结论）

| 主题 | 现状 | 文件:行 |
|------|------|---------|
| CLI task 命令 | 仅有 `task list/show/logs`；无 `start/watch/resume/status` | `cli/main.py:247-286` |
| WorkerRunner | 各 `run_*` 同步调 `client.launch_worker`，不经过 WorkerMonitor | `mcp/worker_runner.py:315-349, 357/441/526` |
| CaoClient | `launch_worker` 阻塞同步；`get_terminal_status/output` 已就绪但未被执行路径用 | `mcp/cao_client.py:155-175, 223-333` |
| WorkerMonitor | `start_worker`/`wait_for_stage`/`poll_worker`/`resume_worker` 已实现，**未接入执行路径** | `mcp/worker_monitor.py:236-364` |
| StageStore | `set_handle_status` 已实现，**无调用方**；`begin_stage` 幂等检查已就绪 | `mcp/stage_store.py:263-290, 151-222` |
| PolicyGateway | 构造了 `worker_monitor` 但 `_stage_*` 不用它；STALLED 恢复分支不可达 | `mcp/policy_gateway.py:73, 308-396, 370-388` |
| 状态机 | `APPROVED` 不是终态（→ DRAFT_PR_CREATED）；本轮需改为终态 | `state/machine.py:89` |
| WorkerHandle | `CaoTerminalHandle`/`ProcessHandle`/`WorkerHandle` 已定义 | `worker_monitor.py:69-136` |
| ProcessHandle 启动 | `_start_process_worker` 已用 `start_new_session=True` + 持久日志 | `worker_monitor.py:503-557` |
| acceptance | `_build_gateway`/`_drive_to_terminal` 可复用 | `cli/acceptance.py:244-269, 295-312` |
| profiles | MCP 配置已就绪，无需改 | `profiles/supervisor.md` |

## 3. 设计决策

### 3.1 简化成功路径

`APPROVED` 改为**成功终态**（无后续转移）。新转移表：

```python
TaskState.APPROVED: set(),  # terminal success (本轮)
```

移除 `APPROVED → {PR_CONTENT_READY/DRAFT_PR_CREATED, FAILED}`（这些属于 PR handoff
分支，不在本轮范围）。`FAILED`/`NEEDS_HUMAN` 仍为终态。

核心状态链（本轮）:

```text
CREATED → RESEARCHING → PLANNING → IMPLEMENTING → LOCAL_VERIFYING →
REMOTE_VERIFYING（项目未配置可跳过）→ REVIEWING → CHANGES_REQUESTED/APPROVED
```

Fix 回路保留: `CHANGES_REQUESTED → FIXING → LOCAL_VERIFYING → ... →
INCREMENTAL_REVIEWING → APPROVED/CHANGES_REQUESTED`。

### 3.2 接通 WorkerMonitor 到执行路径

**核心改动**：`PolicyGateway._stage_*` 方法不再直接调 `self.runner.run_xxx`（同步阻塞），
改为经 `WorkerMonitor` 非阻塞驱动：

1. `self.worker_monitor.start_worker(...)` → 返回 `worker_id`，Worker 在后台运行。
2. `self.stages.set_handle_status(task_id, stage, handle_status="RUNNING",
   resume_state=<当前状态>, worker_id=worker_id)` → 持久化到 StageStore。
3. `self.worker_monitor.wait_for_stage(task_id, stall_timeout=cfg.stall_timeout)` →
   阻塞直到 COMPLETED/FAILED/STALLED（工具内部轮询，不需要 Supervisor 模型反复调用）。
4. COMPLETED → 收集结果（`get_terminal_output` + `extract_strict_json`）→
   `complete_stage` + `transition`。
5. STALLED → 先 `resume_worker`（重新挂载原 handle）；失败才 `NEEDS_HUMAN`。

**`wait_for_stage` 轮询条件**（任一满足即继续等待）:
- 输出仍在增长（`output_offset` 变化）
- terminal 状态仍为 PROCESSING/RUNNING
- 进程或子进程仍存活（`os.kill(pid, 0)`）
- CPU 时间或 I/O 计数仍变化
- 测试/构建/Git 子进程仍运行

全部无进展超过 `stall_timeout`（默认 1800s）才标记 STALLED。

**WorkerRunner 改造**：新增 `run_stage_via_monitor(task_id, stage, profile, prompt,
working_directory, ...)` 方法，封装"start_worker → set_handle_status →
wait_for_stage → 收集结果"流程。各 `_stage_*` 方法调它而非直接 `launch_worker`。
原 `run_*` 方法保留为内部辅助（构造 prompt + 解析结果），不再直接调 `client.launch_worker`。

### 3.3 两种 Worker Handle 持久化

**CaoTerminalHandle**（Codex）:
- `terminal_id`、`session_name`、`output_offset`、`last_progress_at`
- 通过 CaoClient run-step 启动，terminal_id 持久化到 `workers` 表

**ProcessHandle**（OpenCode）:
- `pid`、`pgid`、`stdout_log`、`stderr_log`、`exit_code_file`、`last_progress_at`
- `start_new_session=True`（脱离前台），stdout/stderr 写入持久日志文件
- 进程信息持久化到 `workers` 表

所有 handle 保存到 SQLite（`workers.db`），Controller 退出后仍可恢复。

### 3.4 真正实现 resume

`task resume <task-id>`:
1. 从 SQLite 读取原 Stage attempt 和 Worker handle（`StageStore.get` +
   `WorkerMonitor.find_for_task`）。
2. 复用同一 `terminal_id`（CAO）或 `pid`（process）。
3. 不重复调用 Planner/Executor（StageStore 幂等：COMPLETED 跳过）。
4. 不重复消费 Codex 预算（`CodexBudget` 记录已花费）。
5. 不重复创建 branch 或 commit（worktree 已存在，`create_task_branch` 幂等）。
6. Worker 已完成时只收集原结果（`get_terminal_output` + 解析）。
7. Worker 仍运行时继续等待（`wait_for_stage`）。

**不允许**通过"重新运行同一 Stage"伪装恢复。

### 3.5 CLI 命令

#### `task start`

```bash
supervisor-cao task start \
  --repo /path/to/repo \
  --base-branch main \
  --description-file task.md
```

- 读取 `--description-file` 获取任务描述。
- 创建 task（`PolicyGateway.create_task`）。
- **默认持续等待任务完成**（循环 `run_next_stage` 直到终态）。
- 按 Ctrl+C 只退出前台监控，**不杀 Worker**（Worker 在后台 `start_new_session` 运行）。
- Ctrl+C 后打印 `task resume <task-id>` 提示。

#### `task watch`

```bash
supervisor-cao task watch <task-id> [--json] [--follow] [--poll-interval 5] [--stall-timeout 1800]
```

持续显示:
- 当前 Stage
- Worker 类型（CaoTerminal / Process）
- terminal_id 或 pid
- 已运行时间
- 最后进展时间
- 最新输出摘要（默认只显示新增输出，不重复打印全部日志）
- candidate/tested/reviewed SHA
- Codex 预算

`--json`: 每次轮询输出一行 JSON（供程序化消费）。
`--follow`: 持续轮询直到终态（类似 `tail -f`）。
`--poll-interval`: 轮询间隔秒数（默认 5）。
`--stall-timeout`: STALLED 超时秒数（默认 1800）。

#### `task resume`

```bash
supervisor-cao task resume <task-id>
```

见 §3.4。默认持续等待到终态（同 `task start`）。

#### `task status`

```bash
supervisor-cao task status <task-id>
```

一次性输出当前状态快照（不轮询）。

#### `task logs`

已有，改为支持 `--follow` 和只显示新增输出。

### 3.6 max_runtime: null

`ProjectConfig.executor_limits.max_runtime` 默认改为 `null`（无固定总超时）。
`CaoClient.launch_worker` 的 `timeout=None` 已用 86400s 安全上界（`cao_client.py:196`）。
`WorkerMonitor.wait_for_stage` 用 `stall_timeout`（progress-based）替代固定总超时。

### 3.7 Acceptance 三场景

新增 `supervisor-cao acceptance runtime-direct`、`runtime-review-fix`、
`runtime-resume`（复用现有 `_build_gateway` + 真实 CaoClient，不用 fake/mock）。

使用 `/root/projects/supervisor-cao-live-test` 作为测试 repo。

**direct**: 真实完成 `parse_duration` 任务，最终状态 `APPROVED`。
**review-fix**: 真实缺陷 → CHANGES_REQUESTED → fix → APPROVED。
**resume**: 真实 controller restart + Worker reattach。

每条场景保存完整 evidence（task 事件、Worker handles、Stage attempts、stdout/stderr
日志、Codex 调用、SHA、Git commit/branch、Review 决定）。

## 4. 涉及修改的文件

| 文件 | 改动 |
|------|------|
| `src/supervisor_cao/cli/main.py` | 新增 `task start/watch/resume/status` 命令 |
| `src/supervisor_cao/cli/task_runner.py` | **新建**: task 命令驱动逻辑（复用 `_drive_to_terminal` + `wait_for_stage`） |
| `src/supervisor_cao/mcp/policy_gateway.py` | `_stage_*` 改为经 WorkerMonitor 驱动；APPROVED 终态 |
| `src/supervisor_cao/mcp/worker_runner.py` | 新增 `run_stage_via_monitor`；原 `run_*` 保留为 prompt 构造+结果解析 |
| `src/supervisor_cao/mcp/worker_monitor.py` | 微调 `wait_for_stage` 轮询条件（CPU/IO/子进程检测） |
| `src/supervisor_cao/mcp/stage_store.py` | `set_handle_status` 接入执行路径 |
| `src/supervisor_cao/state/machine.py` | `APPROVED` 改为终态 |
| `src/supervisor_cao/cli/acceptance.py` | 新增 `runtime-direct`/`runtime-review-fix`/`runtime-resume` |
| `src/supervisor_cao/projects/config.py` | `max_runtime` 默认 null |
| `tests/unit/test_task_cli.py` | **新建**: task 命令测试 |
| `tests/unit/test_worker_monitor_integration.py` | **新建**: WorkerMonitor 接入执行路径测试 |
| `tests/unit/test_state_machine.py` | APPROVED 终态测试 |
| `docs/TASK_CLI.md` | **新建**: task 子命令用户文档 |

## 5. 实施顺序（TDD）

1. 状态机: APPROVED 终态 + 测试
2. WorkerMonitor 接入: `run_stage_via_monitor` + `set_handle_status` + 测试
3. PolicyGateway: `_stage_*` 改为经 monitor 驱动 + 测试
4. CLI: `task start/watch/resume/status` + 测试
5. acceptance: `runtime-direct`/`runtime-review-fix`/`runtime-resume`
6. 文档 + 完整回归 + push

## 6. 约束

- 不做 PR/Windows同步/forge API
- 不合并或修改 `feat/pr-content-handoff`
- `APPROVED` 即任务成功终态
- 不用 fake CaoClient 或 local_fixture 做 live acceptance
- 三条 runtime acceptance 全部通过前状态保持 `READY_WITH_KNOWN_LIMITATIONS`
- Ctrl+C 不杀 Worker
