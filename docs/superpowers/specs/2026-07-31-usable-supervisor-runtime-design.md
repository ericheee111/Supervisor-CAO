# Usable Supervisor Runtime 设计文档（修订版）

**日期**: 2026-07-31
**分支**: `feat/usable-supervisor-runtime`
**状态**: 设计已批准（用户 2026-07-31 确认 11 条修订意见），进入实现
**基线**: `main` (commit `ee09ac7`)

## 1. 背景与目标

Supervisor-CAO 的运行时"骨架"已存在，但**执行路径存在断点**：`WorkerRunner` 各
`run_*` 方法同步直调 `CaoClient.launch_worker`，完全绕过了已实现但未接入的
`WorkerMonitor`，导致 progress-based 无超时监控、STALLED 重新挂载、崩溃后 worker
续跑等能力均不可用。CLI 缺少 `task start/watch/resume` 等长任务命令。

**本轮目标**：让 Supervisor 真正可用。用户只需 5 个 CLI 命令即可驱动完整任务闭环。
不涉及 PR handoff、Windows 同步或 forge API——runtime CLI 把 `APPROVED` 列为本轮
成功终态。

**非目标**: 不做 PR 创建/内容包/Windows 同步/forge API；不合并或继续修改
`feat/pr-content-handoff`；不全局删除 `APPROVED` 的后续转移（状态机可保留可选后处理）。

## 2. 现状映射（调研结论）

| 主题 | 现状 | 文件:行 |
|------|------|---------|
| CLI task 命令 | 仅有 `task list/show/logs`；无 `start/watch/resume/status` | `cli/main.py:247-286` |
| WorkerRunner | 各 `run_*` 同步调 `client.launch_worker`，两套执行入口含糊 | `mcp/worker_runner.py:315-349, 357/441/526` |
| CaoClient run-step | 放在 daemon 线程中，Controller 退出后线程和结果丢失；terminal_id 可能是 `unknown-*` | `mcp/cao_client.py:223-333`, `worker_monitor.py:426-477` |
| WorkerMonitor | `start_worker`/`wait_for_stage`/`poll_worker`/`resume_worker` 已实现，**未接入执行路径** | `mcp/worker_monitor.py:236-364` |
| ProcessHandle reaper | `_start_process_worker` 用 daemon reaper 线程写 exit code，Controller 退出后丢失 | `worker_monitor.py:503-557` |
| StageStore | `set_handle_status` 已实现，**无调用方**；幂等键缺少 `input_sha` | `mcp/stage_store.py:263-290, 151-222` |
| PolicyGateway | 构造了 `worker_monitor` 但 `_stage_*` 不用它；STALLED 恢复分支不可达 | `mcp/policy_gateway.py:73, 308-396, 370-388` |
| 状态机 | `APPROVED` 非终态（→ DRAFT_PR_CREATED 等） | `state/machine.py:89` |
| ProjectConfig | 无 config 快照持久化；resume 会重新加载可变配置 | `projects/config.py` |
| remote verification | 无 disabled/optional/required 模式定义 | `policy_gateway.py:518` |
| owner lease | `DEFAULT_LEASE_DURATION=300s`；无 `owner_pid` 存活检查；无双 Controller 互斥 | `worker_monitor.py:57, 704-735` |

## 3. 设计决策

### 3.1 统一 monitor 封装（不留两套 run_* 入口）

废弃 `WorkerRunner.run_*` 作为生产执行入口。生产路径**只有 WorkerMonitor 可以启动
Worker**。新的四阶段流程：

```text
build_<stage>_request(task_id, ...) -> StageRequest
start_stage(task_id, stage, request) -> worker_id    # WorkerMonitor.start_worker
wait_for_stage(task_id, stall_timeout) -> WorkerResult  # 阻塞轮询
finalize_<stage>_result(task_id, worker_result) -> artifact  # 解析+验证+落盘
```

- `build_<stage>_request`: 纯函数，构造 prompt + 参数（profile/working_directory/
  session_name/model/timeout）。不启动 Worker，不调网络。
- `start_stage`: 调 `WorkerMonitor.start_worker`，返回 `worker_id`，Worker 在后台运行。
  写 `StageStore.set_handle_status(handle_status="RUNNING", resume_state=<当前状态>,
  worker_id=worker_id)`。
- `wait_for_stage`: 调 `WorkerMonitor.wait_for_stage`，阻塞直到 COMPLETED/FAILED/STALLED。
  工具内部持续轮询，不需要 Supervisor 模型反复调用。
- `finalize_<stage>_result`: 从 worker-shim 的 `result.json` 或 `get_terminal_output`
  收集结果 → `extract_strict_json` → `validate_and_stamp` → `_save_artifact` →
  `complete_stage` + `transition`。

`WorkerRunner` 重构为 `StageRequestBuilder`（prompt 构造）+ `ResultFinalizer`（结果解析），
不再持有 `CaoClient` 或直接调 `launch_worker`。原 `run_*` 方法删除或改为 `build_*`/
`finalize_*`。

### 3.2 Worker-shim 持久化（OpenCode + Codex 统一）

**不再依赖 daemon 线程完成结果收集。** OpenCode 和 Codex 都通过独立持久 worker-shim
运行：

**worker-shim** 是一个独立 Python 进程（`scripts/worker-shim`），负责：
1. 启动实际 Worker（OpenCode CLI 或 Codex run-step）
2. 持久化进程信息到 SQLite（`workers` 表）
3. 将 stdout/stderr 写入持久日志文件
4. Worker 完成后写 `result.json` + `exit-code` 文件
5. 自行退出（不依赖 Controller 的 daemon reaper）

**持久化字段**（`workers` 表，已存在 + 新增）:
- `pid`、`pgid`（`start_new_session=True`，脱离前台）
- `stdout_log`、`stderr_log`（持久日志文件路径）
- `result_json`（结果文件路径）
- `exit_code_file`
- `last_progress_at`
- `owner_id`、`owner_pid`（Controller PID，用于崩溃后存活检查）
- `lease_until`

**Codex 异步启动修复**：
- **不**放在 daemon 线程中跑 run-step。
- worker-shim 内部先创建 CAO terminal 取得真实 `terminal_id`（不是 `unknown-*`），
  再提交 run-step 到该 terminal。
- 如果 run-step 不支持预创建 terminal，worker-shim 直接用 `subprocess.Popen` 启动
  `codex` CLI（与 OpenCode 同路径），结果写 `result.json`。
- Controller 退出后 worker-shim 继续运行，结果已落盘，resume 时直接读 `result.json`。

### 3.3 状态机：不全局删除 APPROVED 后续转移

`APPROVED` 的后续转移（`→ PR_CONTENT_READY/DRAFT_PR_CREATED` 等）**保留在状态机中**
（状态机可保留可选后处理）。但 runtime CLI 把 `APPROVED` 列为**本轮成功终态**——
`task start`/`task watch`/`task resume` 到达 `APPROVED` 即停止驱动，不继续后处理。

```python
# 状态机不变：
TaskState.APPROVED: {TaskState.PR_CONTENT_READY, TaskState.FAILED},  # 保留
# runtime CLI 终态判断：
RUNTIME_TERMINAL = {TaskState.APPROVED.value, TaskState.FAILED.value,
                    TaskState.NEEDS_HUMAN.value}
```

### 3.4 ProjectConfig 快照持久化

`task start` 必须支持 `--project`（指定项目名），或在临时 repo 模式要求 `--verify-command`。

**启动时持久化解析后的完整 ProjectConfig 快照**:
- `task start` 解析 config（合并 builtin → example → local → override）后，
  将完整 `ProjectConfig.to_dict()` 写入 `<run-dir>/config-snapshot.json`。
- `task resume` **不得重新加载可变配置**——从 `config-snapshot.json` 读取，确保
  resume 行为与 start 时一致。
- `PolicyGateway` 在 resume 时从快照加载 config，不从 `~/.config/` 重新读。

### 3.5 task watch / logs --follow 只读

`task watch` 和 `task logs --follow` **必须只读**：
- 使用新增的 `WorkerMonitor.peek_worker(worker_id)` 方法（只读轮询，不获取/续订
  owner lease）。
- **不得**调用 `start_worker`/`wait_for_stage`/`resume_worker`（这些会获取 lease）。
- **不得**修改任何状态、Stage、Worker handle。
- 只读取: 当前 state、Stage status、Worker status、output_offset、last_progress_at、
  stdout/stderr 日志新增部分、SHA、budget。

### 3.6 Ctrl+C 与 owner lease 安全

**Ctrl+C 时**:
- 释放 Controller 的 owner lease（`WorkerMonitor.release_ownership(worker_id)`）。
- **不杀 Worker**（Worker 在 `start_new_session` 的独立进程组中，不受 Ctrl+C 影响）。
- 打印 `task resume <task-id>` 提示。

**异常崩溃后安全接管**:
- `workers` 表新增 `owner_pid` 字段（Controller 进程 PID）。
- resume 时检查 `owner_pid` 是否存活（`os.kill(pid, 0)` / `OpenProcess`）。
- 若 `owner_pid` 已死且 lease 过期 → 安全接管（claim ownership）。
- 若 `owner_pid` 仍存活且 lease 未过期 → **拒绝接管**（两个活跃 Controller 不得同时
  拥有同一 handle）。
- 若 `owner_pid` 仍存活但 lease 过期 → 警告（可能双 Controller），仍拒绝接管，进入
  `NEEDS_HUMAN`。

**双 Controller 互斥**: `start_stage`/`wait_for_stage`/`resume_worker` 在获取 lease 前
检查 `owner_pid` 存活性。lease 未过期且 owner 存活时，其他 Controller 无法获取。

### 3.7 真正取消总时限

```python
max_runtime = None   # 无固定总超时
max_polls = None     # 无轮询次数上限
```

- `WorkerMonitor.wait_for_stage` 的 `max_polls` 默认改为 `None`（无限轮询）。
- `PROCESSING` 只算 **liveness**（Worker 还活着），**不算每次都有新 progress**。
- **Progress** 必须来自以下之一的变化:
  - 输出增长（`output_offset` 变化）
  - CPU 时间变化（`os.times`/`psutil`）
  - I/O 计数变化（`/proc/<pid>/io` 或 `psutil.Process.io_counters()`）
  - 子进程仍运行（`os.kill(pgid, 0)` 或枚举子进程）
  - provider 更新时间变化（CAO `last_active`）
- 全部无 progress 超过 `stall_timeout`（默认 1800s）才标记 STALLED。

### 3.8 Stage 幂等键

```text
task_id + stage + attempt + input_sha
```

- `input_sha`: stage 输入的 SHA（如 plan.json 的 SHA、candidate_sha 等）。
- `StageStore.begin_stage` 已有 `input_sha` 参数和 `attempt` 字段——需确保所有
  `_stage_*` 调用时传入 `input_sha`。
- `Codex call_id` 和 `worker_id` 必须持久化到 `stage_runs` 表（字段已存在但未写入）。
- Resume 时:
  - COMPLETED + 同 `input_sha` → 跳过（不重复预算/Worker/branch/commit）
  - COMPLETED + 不同 `input_sha` → reclaim（attempt++，重新运行）
  - RUNNING + owner 存活 → 继续 `wait_for_stage`
  - RUNNING + owner 死亡 + lease 过期 → 安全接管，继续 `wait_for_stage`
  - RUNNING + owner 死亡 + lease 未过期 → NEEDS_HUMAN

### 3.9 Remote verification mode

明确定义三种模式:

| 模式 | 行为 |
|------|------|
| `disabled` | 跳过 remote verification，直接从 LOCAL_VERIFIED 到 REVIEWING |
| `optional` | 尝试 remote verification，失败则 fallback 到 LOCAL_VERIFIED（默认） |
| `required` | remote verification 必须通过，失败则 FAILED |

- `ProjectConfig.remote_validation.mode`（新增字段，默认 `optional`）。
- `disabled` 跳过时写 **skip artifact**（`verification-remote.json`:
  `{"skipped": true, "reason": "disabled"}`）和 **audit 事件**
  (`REMOTE_VERIFICATION_SKIPPED`)。
- `optional` 失败时写 fallback artifact + audit 事件。
- `required` 失败时 `transition(FAILED)`。

### 3.10 CLI 命令

#### `task start`

```bash
supervisor-cao task start \
  --repo /path/to/repo \
  --base-branch main \
  --description-file task.md \
  [--project <name>] \
  [--verify-command "pytest"] \
  [--stall-timeout 1800]
```

- `--project`: 指定项目名（从 config 加载）。若未指定，用临时 repo 模式。
- 临时 repo 模式必须提供 `--verify-command`。
- 解析 config → 持久化 `config-snapshot.json`。
- 创建 task → 持续等待到 `APPROVED`/`FAILED`/`NEEDS_HUMAN`。
- Ctrl+C 释放 lease，不杀 Worker，打印 `task resume`。

#### `task watch`

```bash
supervisor-cao task watch <task-id> [--json] [--follow] [--poll-interval 5]
```

只读（`peek_worker`），持续显示 Stage/Worker/输出/SHA/budget。

#### `task resume`

```bash
supervisor-cao task resume <task-id> [--stall-timeout 1800]
```

从 `config-snapshot.json` 加载配置。从 SQLite 读取 handle。安全接管。继续到终态。

#### `task status` / `task logs`

`status`: 一次性快照。`logs [--follow]`: 只读 tailing。

### 3.11 Acceptance 三场景

新增 `supervisor-cao acceptance runtime-direct`、`runtime-review-fix`、
`runtime-resume`。**repo 路径通过 CLI 参数传入，不硬编码**:

```bash
supervisor-cao acceptance runtime-direct --repo /root/projects/supervisor-cao-live-test
```

- `--repo` 必填，指定 live test repo 路径。
- 不用 fake CaoClient 或 local_fixture。
- 每条场景保存完整 evidence（task 事件、Worker handles、Stage attempts、stdout/stderr
  日志、Codex 调用、SHA、Git commit/branch、Review 决定）。

## 4. 涉及修改的文件

| 文件 | 改动 |
|------|------|
| `src/supervisor_cao/cli/main.py` | 新增 `task start/watch/resume/status` |
| `src/supervisor_cao/cli/task_runner.py` | **新建**: task 命令驱动逻辑 |
| `scripts/worker-shim` | **新建**: 独立持久 Worker 启动器 |
| `src/supervisor_cao/mcp/worker_runner.py` | 重构为 `build_*`/`finalize_*`，删除 `run_*` 执行入口 |
| `src/supervisor_cao/mcp/worker_monitor.py` | worker-shim 集成；`peek_worker`；`owner_pid`；取消 max_polls；progress 检测增强 |
| `src/supervisor_cao/mcp/stage_store.py` | `input_sha` 幂等键；`codex_call_id`/`worker_id` 写入 |
| `src/supervisor_cao/mcp/policy_gateway.py` | `_stage_*` 改为四阶段流程；config 快照加载；remote verification mode |
| `src/supervisor_cao/state/machine.py` | 保留 APPROVED 后续转移；runtime 终态判断 |
| `src/supervisor_cao/projects/config.py` | `remote_validation.mode`；`max_runtime=None` |
| `src/supervisor_cao/cli/acceptance.py` | `runtime-*` 场景 + `--repo` 参数 |
| `tests/unit/test_task_cli.py` | **新建** |
| `tests/unit/test_worker_monitor_integration.py` | **新建** |
| `tests/unit/test_worker_shim.py` | **新建** |
| `docs/TASK_CLI.md` | **新建** |

## 5. 实施顺序（TDD）

1. worker-shim 持久化 + 测试
2. WorkerMonitor 改造（peek_worker/owner_pid/取消 max_polls/progress 增强）+ 测试
3. WorkerRunner 重构（build_*/start_stage/wait_for_stage/finalize_*）+ 测试
4. StageStore 幂等键（input_sha/call_id/worker_id）+ 测试
5. PolicyGateway 四阶段流程 + remote verification mode + 测试
6. CLI task start/watch/resume/status + 测试
7. acceptance runtime-direct/review-fix/resume + --repo 参数
8. 文档 + 完整回归 + push

## 6. 约束

- 不做 PR/Windows同步/forge API
- 不合并或修改 `feat/pr-content-handoff`
- runtime CLI 把 APPROVED 列为本轮成功终态（状态机保留后续转移）
- 不用 fake CaoClient 或 local_fixture 做 live acceptance
- repo 路径通过 CLI 参数传入，不硬编码
- Ctrl+C 不杀 Worker，释放 lease
- task watch/logs 只读，不获取 lease
- 三条 runtime acceptance 全部通过前保持 `READY_WITH_KNOWN_LIMITATIONS`
- 生产路径只有 WorkerMonitor 可以启动 Worker
- 结果写入不依赖 daemon reaper 线程

## 7. 已确认决策记录（用户 2026-07-31 批复 11 条）

1. 统一 monitor 封装：`build_*`/`start_stage`/`wait_for_stage`/`finalize_*`，只有
   WorkerMonitor 启动 Worker。
2. Codex 异步启动修复：worker-shim 持久化，不依赖 daemon 线程。
3. OpenCode/Codex 统一 worker-shim：pid/pgid/stdout/stderr/result.json/exit-code。
4. 不全局删除 APPROVED 后续转移；runtime CLI 列为本轮终态。
5. task start 支持 --project 或 --verify-command；持久化 config 快照。
6. task watch/logs --follow 只读，用 peek_worker。
7. Ctrl+C 释放 lease 不杀 Worker；owner_pid 存活检查安全接管；双 Controller 互斥。
8. max_runtime=None, max_polls=None；PROCESSING 只算 liveness；progress 来自输出/CPU/IO/子进程/provider。
9. 幂等键 task_id+stage+attempt+input_sha；codex_call_id/worker_id 持久化。
10. remote verification mode: disabled/optional/required；跳过写 skip artifact+audit。
11. live acceptance repo 路径通过 CLI 参数传入。
