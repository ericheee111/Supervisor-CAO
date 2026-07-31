# Forge-agnostic PR Handoff 设计文档

**日期**: 2026-07-31
**分支**: `feat/pr-content-handoff`
**状态**: 设计待批准
**输入**: 用户 2026-07-31 规格书（pasted-text-20260731-174617-0c8fef1e.txt）

## 1. 背景与目标

当前平台在 `APPROVED` 后通过 `scripts/create-draft-pr` 调用 `gh pr create` 创建真实
GitHub Draft PR，并以 `DRAFT_PR_CREATED` 作为 Windows sync 的门禁。这把平台的终态
与 GitHub forge 耦合：GitCode / GitLab / 普通 SSH remote 都无法走完收口；`draft_pr_created`
gate 实际只是硬编码 `True`（`windows_sync.py:84` + `policy_gateway.py:272/722`），并未真正
校验 PR 存在。

本轮目标：把 forge 依赖从生产路径彻底剥离，改为生成一个**可复制的 PR 内容包**
（`pr-content.{json,md,sha256}`），用户自行粘贴到任意 forge 创建 PR。同步门禁改为校验
该内容包 artifact，而非查询 forge API。同时重做三条真实验收场景，使证据 append-only、
断言严格、resume 真正反映 mid-stage 中断恢复。

**非目标**: 不新增 WorkerMonitor / Judge 功能；不自动创建任何平台 PR；不改动上游 CAO。

## 2. 现状映射（调研结论）

| 主题 | 现状 | 文件:行 |
|------|------|---------|
| 状态枚举 | 含 `DRAFT_PR_CREATED` (L47) | `state/machine.py:46-49` |
| 转移表 | `APPROVED -> {DRAFT_PR_CREATED, FAILED}` (L89)；`DRAFT_PR_CREATED -> {WINDOWS_SYNCED, FAILED, READY_FOR_HUMAN_REVIEW}` (L90，含直跳) | `machine.py:89-91` |
| ErrorState | `PR_CREATION_FAILED` (L64) | `machine.py:55-66` |
| PolicyGateway.create_draft_pr | 仅校验 APPROVED + SHA，返回 status，不建 PR | `mcp/policy_gateway.py:243-255` |
| _stage_draft_pr | 子进程调 `scripts/create-draft-pr`（含 `gh pr create`） | `policy_gateway.py:679-703` |
| sync_windows / _stage_windows_sync | 调 `win_sync(..., draft_pr_created=True)` 硬编码 | `policy_gateway.py:259-276, 705-727` |
| SyncGates | `draft_pr_created: bool` 是入参，无实际查询 | `validation/windows_sync.py:42-87` |
| create-draft-pr 脚本 | `gh pr list/edit/create`，要求 origin 是 GitHub | `scripts/create-draft-pr:102-134, 229-234` |
| acceptance direct | `test_mode=False`，要求 `draft_pr_url` 以 `https://` 开头 | `cli/acceptance.py:315-352` |
| acceptance review-fix | success 仅看 `protocol_passed`，不看 `task_approved`/终态/PR包 | `acceptance.py:475-492` |
| acceptance resume | `candidate_unchanged` 是 tautology 且不参与 `ok`；`budget_not_respent` 用 `>=` | `acceptance.py:548, 563-570` |
| acceptance cleanup | `rmtree(ACCEPTANCE_ROOT)` + `gh pr close --delete-branch` + `git push --delete acc/*` | `acceptance.py:601-663` |
| evidence 覆盖 | `_record_scenario` 覆写 meta.scenarios[name] | `acceptance.py:122-125` |
| MCP server | 只注册 5 个 tool；`create_draft_pr`/`sync_windows` 非 tool | `mcp/server.py` |
| StageStore / WorkerMonitor | `resume_state` 已存在于 stage_runs / workers | `stage_store.py:74`, `worker_monitor.py:110` |

## 3. 设计决策

### 3.1 命名替换映射（全仓统一）

| 旧 | 新 |
|----|----|
| `scripts/create-draft-pr` | `scripts/render-pr-content` |
| `TaskState.DRAFT_PR_CREATED` | `TaskState.PR_CONTENT_READY` |
| `stage: "draft_pr"` | `stage: "pr_content"` |
| `gate: draft_pr_created` | `gate: pr_content_ready` |
| `ErrorState.PR_CREATION_FAILED` | `ErrorState.PR_CONTENT_GENERATION_FAILED` |
| `PolicyGateway.create_draft_pr()` | `PolicyGateway.prepare_pr_content()` |
| `_stage_draft_pr` | `_stage_pr_content` |
| `draft-pr-url.txt` | `pr-content.json` + `pr-content.md` + `pr-content.sha256` |

### 3.2 PR 内容包 schema

**`pr-content.json`**（确定性顺序，便于 diff 与幂等）:

```jsonc
{
  "schema_version": 1,
  "task_id": "...",
  "title": "[Draft] <task-id>",
  "base_branch": "main",
  "head_branch": "agent/<task-id>",
  "candidate_sha": "...",
  "tested_sha": "...",
  "reviewed_sha": "...",
  "changed_files": ["..."],
  "plan_summary": "...",
  "local_verification": {...},
  "remote_verification": {...},
  "review_decision": "APPROVED",
  "review_findings": [...],
  "codex_call_count": 0,
  "codex_budget": {...},
  "known_risks": [...],
  "artifact_paths": ["plan.json", "..."],
  "generated_at": "<ISO8601 UTC>",
  "rendered_sha256": "<sha256 of pr-content.md>"
}
```

**`pr-content.md`**: 人类可读 Markdown body（即 PR Body），含上述字段的表格化呈现
+ 完整 changed files 列表 + plan summary + verification 结果 + review findings + risks
+ artifact manifest 与各 artifact 的 SHA-256。

**`pr-content.sha256`**: `pr-content.md` 的 SHA-256 hex（单行），用于幂等校验。

**stdout 契约**（`scripts/render-pr-content`）:

```
PR Title:
<title>

Base:
<base>

Head:
<head>

PR Body:
<markdown body>
```

### 3.3 校验门禁（生成前）

`scripts/render-pr-content` 在生成前必须通过（与现 `create-draft-pr` L171-208 一致并加强）:

1. 5 个 artifact 全部存在并可 JSON 解析: `plan.json` / `implementation.json` /
   `verification.json` / `review.json` / `codex-budget-summary.json`。
2. `candidate_sha == tested_sha == reviewed_sha`（任一缺失或不等 → exit 1）。
3. `review.decision == "APPROVED"`。
4. candidate 已 push 到 task branch（通过 `git rev-parse origin/<task_branch>` 校验，
   **不要求 origin 是 GitHub**；任何能 `git rev-parse` 的 remote 均可）。

PolicyGateway.`prepare_pr_content()` 在调用脚本前再做一次状态层校验:
`rec.state == APPROVED` 且 `reviewed_sha == candidate_sha`。

### 3.4 幂等性

- 相同 candidate 重复调用 `render-pr-content` 必须产出**字节相同**的 `pr-content.{json,md}`。
  - `generated_at` 字段会造成字节差异 → **从 `pr-content.json` 与 `pr-content.md` 中
    移除时间戳**；时间戳只写一个独立的 `pr-content.meta.json`（非内容包组成部分，
    不参与 sha256，不参与幂等校验）。这样内容包本身是 pure function of (artifacts, task_id)。
  - 或：保留 `generated_at` 但用 candidate_sha 派生（确定性）。**决策：移除时间戳**，
    更干净，幂等校验直接 `cmp` 文件即可。
- 重复调用不产生副作用（脚本只读 artifact + git rev-parse，无网络、无写 DB、无 push）。
- StageStore 的 `begin_stage` 幂等检查（COMPLETED + 同 candidate_sha → skip）继续生效。

### 3.5 状态机迁移

新转移表（`machine.py`）:

```python
TaskState.APPROVED: {TaskState.PR_CONTENT_READY, TaskState.FAILED},
TaskState.PR_CONTENT_READY: {TaskState.WINDOWS_SYNCED, TaskState.FAILED},  # 移除直跳 READY
TaskState.WINDOWS_SYNCED: {TaskState.READY_FOR_HUMAN_REVIEW, TaskState.FAILED},
```

- 移除 `PR_CONTENT_READY -> READY_FOR_HUMAN_REVIEW` 直跳（旧 `DRAFT_PR_CREATED` 有此
  直跳，允许跳过 windows sync，是漏洞）。
- Windows sync 是强制门禁（除非项目未配置 `windows_repo`，此时 `_stage_windows_sync`
  保持现有 skip 逻辑，但记录 event 说明跳过原因）。

### 3.6 Legacy 兼容

- `TaskState.DRAFT_PR_CREATED` 枚举值**保留**（不删除），但从 `TRANSITIONS` 中移除其
  正向出口，使其成为"历史遗留终态"。
- StateStore 读取到 `DRAFT_PR_CREATED` 时，提供 `migrate_legacy_state()`:
  - 若 `pr-content.json` 已存在 → 迁移为 `PR_CONTENT_READY`，记录 event
    `legacy_migration: DRAFT_PR_CREATED -> PR_CONTENT_READY`。
  - 若不存在 → 迁移为 `APPROVED`（回退一态，允许重新生成内容包），记录 event。
- 新任务**永远不能进入** `DRAFT_PR_CREATED`（转移表无入口）。
- 旧 `create_draft_pr` 公共方法保留为 **deprecated wrapper**:
  ```python
  def create_draft_pr(self, task_id, project):
      """DEPRECATED: use prepare_pr_content. Does NOT access network."""
      import warnings; warnings.warn("use prepare_pr_content", DeprecationWarning, stacklevel=2)
      return self.prepare_pr_content(task_id, project)
  ```
  绝不调用网络。

### 3.7 Windows sync 门禁改造

`SyncGates.draft_pr_created: bool` → `SyncGates.pr_content_ready: bool`。

`check_gates()` 不再接受 bool 入参，改为**实际校验** `pr-content.json`:
- 读取 `<run-dir>/pr-content.json`，校验存在 + 可解析 + `candidate_sha` 字段 == 当前
  candidate_sha + `pr-content.sha256` 与 `pr-content.md` 实际 sha256 一致。
- 任一失败 → `pr_content_ready=False`。

`PolicyGateway.sync_windows` / `_stage_windows_sync`:
- 调用 `win_sync` 前**重新加载并验证** `pr-content.json`（不传常量 True）。
- 校验失败 → `PolicyError("WINDOWS_SYNC_BLOCKED: pr-content artifact invalid")`。

### 3.8 Acceptance 三场景重做

#### direct

新通过条件（全部满足）:
- `final_state == READY_FOR_HUMAN_REVIEW`
- `candidate == tested == reviewed`
- `pr-content.json` 有效（存在 + 可解析 + schema 字段齐全 + sha256 一致）
- `pr-content.md` 有效
- `pr-content.sha256` 有效
- **未执行任何 forge API**（脚本进程内无 `gh`/`requests`/`urllib` 调用；通过
  `subprocess` 检查或代码审查断言。实现上：`render-pr-content` 不 import
  `requests`/`urllib`/`gh`，测试用 monkeypatch 断言无网络调用）

移除 `test_mode=False` 的 `gh pr create` 路径与 `is_real_pr` 断言。

#### review-fix

PASS 条件（全部满足）:
- `protocol_passed == true`（保留: had_changes_requested and had_fix and
  had_incremental_review）
- `task_approved == true`（终态 == `READY_FOR_HUMAN_REVIEW`）
- `final_state == READY_FOR_HUMAN_REVIEW`
- PR 内容包有效（同 direct 校验）

Judge 正确进入 `NEEDS_HUMAN` → 记录为 `safety_behavior_evidence`，但 `task_approved=False`
→ 不冒充 PASS。

**受控 candidate 注入**: 现有 `sqlite3 UPDATE tasks` 直写 (L451-456) 违反"不绕过状态机"。
改为通过 `StateStore.inject_candidate(task_id, new_sha, from_state=LOCAL_VERIFYING)` 方法:
- 内部走 `transition()` 合法路径或显式审计入口
- 记录 event `controlled_candidate_injection: sha=..., reason=acceptance_review_fix`
- 清空 tested/reviewed_sha（新候选未测未审）
- acceptance helper 调用它而非裸 SQL

#### resume

完全重做，删除现有宽松断言。

**真实 mid-stage 中断恢复流程**:

1. 在独立 controller 进程中启动真实 Planner 或 Executor（通过 `cao-server` 真实 Worker，
   选一个运行时间足够长的任务，使 stage 真正处于 RUNNING 足够久）。
2. 轮询 `StageStore` 直到 `status == RUNNING` 且 `WorkerHandle` 已持久化
   （`worker_id`/`terminal_id`/`owner_id`/`lease_until` 非空）且观察到真实进展
   （`last_progress_at` 更新过）。
3. 记录中断前快照:
   - `stage_attempts_before`: 每个 stage 的 attempt 数
   - `codex_calls_before`: 每个角色的 call_index / total_used
   - `candidate_before`
   - `worker_handle_before` / `resume_state_before`
4. **终止 controller 进程**（`terminate`，不主动杀 Worker —— 让 Worker 成为孤儿，
   由 WorkerMonitor 后续检测 STALLED）。controller 进程退出即模拟崩溃。
5. 新建 `PolicyGateway` + `WorkerMonitor`，复用原 SQLite（同一 `state_dir`）。
6. 调用 `resume_task()`。
7. 驱动到终态。

**精确断言**（全部满足才 PASS）:
- `final_state == READY_FOR_HUMAN_REVIEW`
- 中断前 COMPLETED 的 stage，其 `attempt` 数**完全不增加**
- 已消费的 Codex `call_index`/`total_used` **完全不增加**（用 `==` 而非 `>=`）
- 不产生重复 commit（candidate_sha 不回退；`git log origin/<task_branch>` 无重复）
- 不重复生成 PR 内容包（`pr-content.sha256` 与首次生成一致；StageStore `pr_content`
  stage attempt 不增加）
- 不重复 Windows sync（`windows_sync` stage attempt 不增加）
- `worker_id` / `terminal_id` / `owner_id` / `lease_until` / `resume_state` 在
  reattach 前后有完整证据链（记录到 evidence）

删除 `candidate_unchanged` tautology (L563-565) 与 `budget_not_respent` 的 `>=` (L548)。

### 3.9 Acceptance evidence append-only

**目录结构**:

```
acceptance/evidence/<run-id>/<scenario>/
  result.json
  task_snapshot.json      # tasks 表快照
  events.jsonl           # 全量 events
  stage_attempts.json
  budget_log.json
  worker_handles.json
  sha_info.json
  pr-content.json
  pr-content.md
  pr-content.sha256
  artifact_manifest.json  # 各 artifact 路径 + sha256
```

- `<run-id>` = 时间戳 + scenario（如 `20260731-120000-direct`），保证不覆盖。
- 每次 `run_scenario` 写入新 `<run-id>` 目录，**不覆盖**历史。
- `meta.scenarios[name]` 仍记录最新 result 指针（`evidence_path` + `passed`），但不删除
  旧 evidence。

**cleanup 改造**:
- 默认 cleanup 只清理: `runtime/`、`worktrees/`、task-owned 进程、`acc/` 远程分支。
- **保留** `acceptance/evidence/`。
- 删除所有 `_cleanup_acceptance_prs`（`gh pr list/close`）逻辑。
- 保留 `_cleanup_acceptance_branches`（只删 `acc/` 前缀远程分支，安全）。
- 新增独立 `acceptance purge-evidence` 子命令（显式删除历史 evidence，需 `--force` 确认）。

### 3.10 生产路径禁止 forge API

在 `scripts/render-pr-content` 中:
- 不 `import requests` / `urllib` / `subprocess`（除 `git rev-parse` 校验 push）。
- 不调用 `gh`。
- 不解析 `origin` 是否 GitHub（任何 remote 均可）。

`PolicyGateway.prepare_pr_content()` 同样不调网络。

测试用 monkeypatch 拦截 `subprocess.run` / `socket` 断言无 forge 调用。

## 4. 涉及修改的文件

| 文件 | 改动 |
|------|------|
| `src/supervisor_cao/state/machine.py` | 枚举改名 + 新增 `PR_CONTENT_READY`、`PR_CONTENT_GENERATION_FAILED`；转移表；`migrate_legacy_state()` |
| `src/supervisor_cao/mcp/policy_gateway.py` | `create_draft_pr`→`prepare_pr_content` deprecated wrapper；`_stage_draft_pr`→`_stage_pr_content` 调用 `render-pr-content`；`sync_windows` 重新加载校验 pr-content；`inject_candidate()` 审计入口 |
| `src/supervisor_cao/validation/windows_sync.py` | `SyncGates.draft_pr_created`→`pr_content_ready`；`check_gates` 实际校验 artifact |
| `scripts/render-pr-content` | 新脚本（从 `create-draft-pr` 派生，移除所有 `gh` 调用，输出 PR 内容包 + stdout 契约） |
| `scripts/create-draft-pr` | 保留为 deprecated wrapper 调用 `render-pr-content`（或删除，视测试） |
| `src/supervisor_cao/cli/acceptance.py` | 三场景重写；evidence append-only；cleanup 改造；移除 PR 清理 |
| `src/supervisor_cao/cli/main.py` | 新增 `acceptance purge-evidence` 子命令 |
| `docs/ACCEPTANCE.md` | 重写三场景通过条件 + 状态描述 |
| `docs/WORKFLOW.md` | 状态机图更新 |
| `docs/USER_GUIDE.md` | PR handoff 说明（复制粘贴流程） |
| `README.md` | 终态说明 |
| `AGENTS.md` | 移除 Draft PR 门禁描述 |
| `tests/unit/test_state_machine.py` | 新转移、legacy 迁移、新任务不进 DRAFT_PR_CREATED |
| `tests/unit/test_policy_mcp_protocol.py` | tool 名更新 |
| `tests/unit/test_windows_sync.py` | `pr_content_ready` gate 校验 |
| `tests/unit/test_pr_content.py` | **新增**: schema、校验、幂等、无网络、多 remote 类型 |
| `tests/unit/test_acceptance_evidence.py` | **新增**: append-only、cleanup 保留 |
| `tests/integration/test_workflow.py` | happy path 终态改名 |
| `tests/e2e/test_temp_repo_e2e.py` | PR 内容包生成 + stdout 契约 |

## 5. 测试矩阵（新增/修改）

| 测试 | 类型 | 断言 |
|------|------|------|
| APPROVED → PR_CONTENT_READY 合法 | unit | transition 成功 |
| 新任务不进入 DRAFT_PR_CREATED | unit | 无转移入口 |
| legacy DRAFT_PR_CREATED 可恢复 | unit | migrate 后进入 PR_CONTENT_READY 或 APPROVED |
| 缺任一 artifact 拒绝生成 | unit | exit 1 |
| 三 SHA 不同拒绝 | unit | exit 1 |
| review 非 APPROVED 拒绝 | unit | exit 1 |
| PR 内容生成不调网络 | unit | monkeypatch subprocess/socket 断言 |
| GitHub/GitCode/SSH remote 都能生成 | unit | mock git rev-parse 三种 remote |
| rerun 幂等 | unit | 二次输出字节相同 |
| Windows sync 校验真实 PR artifact | unit | gate 随 artifact 有效性翻转 |
| cleanup 保留 evidence | unit | evidence 目录存在 |
| acceptance history 不覆盖 | unit | 两次 run 两个目录 |
| direct 新通过条件 | e2e | 无 forge API + pr-content 有效 |
| review-fix 严格条件 | e2e | protocol+approved+final+pr-content |
| 真实 controller restart/resume | e2e | attempt/budget/commit/PR-content/sync 不重复 |
| 无重复 budget/stage/commit/PR-content/sync | e2e | 严格 `==` 断言 |

## 6. 执行顺序（TDD）

1. PR content schema 与 `scripts/render-pr-content` + 测试
2. 状态机 + `PolicyGateway.prepare_pr_content` + legacy 迁移 + 测试
3. Windows sync gate 改造 + 测试
4. acceptance evidence append-only + cleanup 改造 + 测试
5. direct 场景重写 + 测试
6. review-fix 场景重写 + `inject_candidate` + 测试
7. 真实 mid-stage resume 重写 + 测试
8. 文档 + 全量回归 + secret scan + CLI smoke

每步: 先写测试（红）→ 实现（绿）→ lint/type → commit。

## 7. 验收

- 全部 unit/integration/e2e(temp-repo) 通过
- secret scan 无命中
- CLI smoke (`supervisor-cao --help`, `acceptance --help`) 正常
- live cao-server 运行 direct/review-fix/resume 三条，各产生 append-only evidence
- 在证据完成前保持 `READY_WITH_KNOWN_LIMITATIONS`
- 最终回复输出可复制的 PR Title / Base / Head / Body

## 8. 风险与未决

- **resume 真实中断**: 需要一个运行时间足够长的真实 stage 才能可靠观察 RUNNING + 进展。
  风险: cao-server 上的 Worker 太快完成，无法在 RUNNING 窗口内终止 controller。
  缓解: 选 Executor stage + 非平凡任务（多文件实现）；或在 acceptance helper 里用
  一个故意慢的 worker profile（仍真实，只是 prompt 要求详细）。
- **legacy 迁移破坏性**: `migrate_legacy_state` 改 DB 状态。已有测试覆盖，但生产库
  若存在 `DRAFT_PR_CREATED` 任务需用户知晓。缓解: 迁移只在该任务被 `get`/`resume`
  时触发（惰性），并在 event 中留痕。
- **inject_candidate**: 引入新的 StateStore 审计入口，需确保只被 acceptance helper
  调用，不被生产路径滥用。缓解: 方法名带 `inject_` 前缀 + docstring 标注 "acceptance
  only" + 单测验证生产 PolicyGateway 不调用它。

## 9. 待用户确认的决策点

以下是我做出的设计决策，请确认或修正:

1. **时间戳移除**: `pr-content.{json,md}` 不含 `generated_at` 以保证幂等；时间戳放独立
   `pr-content.meta.json`。可接受吗？
2. **`create-draft-pr` 脚本处置**: 保留为 deprecated wrapper 调用 `render-pr-content`
   （不删，避免破坏外部调用），还是直接删除？
3. **resume 真实中断方式**: 终止 controller 进程（不杀 Worker，让 WorkerMonitor 检测
   STALLED 后 reattach）—— 是否符合你对"真实 mid-stage resume"的预期？
4. **legacy `DRAFT_PR_CREATED` 迁移策略**: 惰性迁移（读到时迁移）vs 主动批量迁移
   （启动时扫库）。我倾向惰性。
