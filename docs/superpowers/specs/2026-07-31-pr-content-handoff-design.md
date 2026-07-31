# Forge-agnostic PR Handoff 设计文档

**日期**: 2026-07-31
**分支**: `feat/pr-content-handoff`
**状态**: 设计已批准（用户 2026-07-31 确认 10 条修订意见），进入实现
**输入**: 用户 2026-07-31 规格书 + 10 条修订意见

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

所有新代码、状态、测试和文档只使用 `render-pr-content` / `prepare_pr_content` /
`PR_CONTENT_READY`。旧 `create-draft-pr` 保留为 deprecated wrapper（见 §3.6）。

### 3.2 PR 内容包 schema

**`pr-content.json`**（确定性 key 顺序，UTF-8，LF，末尾换行；不含 `generated_at`，
不含 `rendered_sha256`）:

```jsonc
{
  "schema_version": 1,
  "task_id": "...",
  "title": "<task-id>",
  "base_branch": "main",
  "head_branch": "agent/<task-id>",
  "workflow_state": "PR_CONTENT_READY",
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
  "artifact_paths": ["plan.json", "..."]
}
```

字段说明:
- `workflow_state`: 恒为 `"PR_CONTENT_READY"`（生成时任务处于此态）。**不得**写
  `"READY_FOR_HUMAN_REVIEW"`——该终态只由 Windows sync 成功后产生。
- `title`: 不强制包含 `[Draft]`。是否创建 Draft PR 由用户在代码托管平台界面决定。
- 无 `generated_at`、无 `rendered_sha256`。时间戳/attempt/operator 信息由 StageStore、
  事件日志和 acceptance evidence 记录。

**`pr-content.md`**: 人类可读 Markdown body（即 PR Body），UTF-8，LF，末尾换行。含上述
字段的表格化呈现 + 完整 changed files 列表 + plan summary + verification 结果 +
review findings + risks + artifact manifest 与各 artifact 的 SHA-256。

**`pr-content.sha256`**（同时绑定 JSON 和 Markdown，固定两行格式）:

```text
<json-sha256>  pr-content.json
<markdown-sha256>  pr-content.md
```

统一使用 UTF-8、LF、固定 JSON key 顺序和末尾换行，确保幂等。

### 3.3 校验门禁（生成前）

`scripts/render-pr-content` 在生成前必须通过:

1. 5 个 artifact 全部存在并可 JSON 解析: `plan.json` / `implementation.json` /
   `verification.json` / `review.json` / `codex-budget-summary.json`。
2. `candidate_sha == tested_sha == reviewed_sha`（任一缺失或不等 → exit 1）。
3. `review.decision == "APPROVED"`。
4. **push evidence**: 读取 `<run-dir>/push.json`，校验:
   - `push_succeeded == true`
   - `pushed_sha == candidate_sha`
   - `branch == 配置的 task branch`
   - 缺失或不一致 → exit 1

`scripts/render-pr-content` **不访问网络**，不执行 forge API，不调用 `gh`，不查询远端，
不要求 origin 是 GitHub。只做本地 artifact + `push.json` 校验。

PolicyGateway.`prepare_pr_content()` 在调用脚本前再做一次状态层校验:
`rec.state == APPROVED` 且 `reviewed_sha == candidate_sha`。

### 3.4 幂等性

- 相同 candidate 重复调用 `render-pr-content` 必须产出**字节相同**的
  `pr-content.{json,md,sha256}`。内容包是 pure function of (artifacts, task_id,
  base/head branch)。
- 不含时间戳，不依赖运行时环境。
- 重复调用不产生副作用（脚本只读 artifact + push.json，无网络、无写 DB、无 push）。
- StageStore 的 `begin_stage` 幂等检查（COMPLETED + 同 candidate_sha → skip）继续生效。

### 3.5 Push evidence

`git rev-parse origin/<branch>` 不能可靠证明本次候选已经成功 push，也不应由 renderer
查询远端。改为持久化确定性 evidence。

**`push.json`**（Executor 成功 push candidate 后由平台写入 `<run-dir>/push.json`）:

```json
{
  "schema_version": 1,
  "remote": "origin",
  "branch": "agent/<task-id>",
  "pushed_sha": "...",
  "push_succeeded": true
}
```

- 写入时机: Executor stage 完成真实 `git push` 成功后，立即写 `push.json`。
  push 失败 → `push_succeeded: false` 或不写文件（后续 gate 视为未 push）。
- renderer 只进行本地校验（见 §3.3 第 4 点）。
- Windows sync 也会校验 `push.json`（见 §3.8）。

### 3.6 状态机迁移与 Legacy 兼容

新转移表（`machine.py`）:

```python
TaskState.APPROVED: {TaskState.PR_CONTENT_READY, TaskState.FAILED},
TaskState.PR_CONTENT_READY: {TaskState.WINDOWS_SYNCED, TaskState.FAILED},  # 移除直跳 READY
TaskState.WINDOWS_SYNCED: {TaskState.READY_FOR_HUMAN_REVIEW, TaskState.FAILED},
```

- 新增 `TaskState.PR_CONTENT_READY`。
- 新增 `ErrorState.PR_CONTENT_GENERATION_FAILED`。
- 旧 `DRAFT_PR_CREATED` 枚举值**保留**（可解码），但从 `TRANSITIONS` 中移除其正向出口，
  使其成为"历史遗留态"（无新入口、无新出口）。
- 移除 `PR_CONTENT_READY -> READY_FOR_HUMAN_REVIEW` 直跳（旧 `DRAFT_PR_CREATED` 有此
  直跳，允许跳过 windows sync，是漏洞）。Windows sync 是强制门禁（除非项目未配置
  `windows_repo`，此时 `_stage_windows_sync` 保持现有 skip 逻辑，但记录 event 说明跳过原因）。

**Legacy 迁移策略（惰性，且只发生在 `resume_task` / `advance_task` / 专用 migration 操作）**:

- 普通 `get_task` 读取**不得修改数据库**。
- 仅 `resume_task`、`advance_task`（即 `run_next_stage`）、或专用 `migrate_legacy_state()`
  操作会触发迁移。
- 旧 `DRAFT_PR_CREATED` 任务被 resume/advance 时:
  - **artifact 完整**（5 artifact + push.json 齐全且校验通过）: 在**单一事务**中生成内容包
    并迁移到 `PR_CONTENT_READY`，记录 `LEGACY_STATE_MIGRATED` 事件。
  - **artifact 不完整**: **不得静默退回 `APPROVED`**，也**不得伪造成功**。保持原状态
    `DRAFT_PR_CREATED`，返回明确 migration error（列出缺失项），或进入 `NEEDS_HUMAN`
    并记录缺失项。
- `DRAFT_PR_CREATED` 不作为永久成功终态。

**旧 `create_draft_pr` 公共方法 + `scripts/create-draft-pr`**: 保留为 deprecated wrapper:
- 只调用 `render-pr-content` / `prepare_pr_content`。
- 输出弃用提示（stderr）。
- **不调用 `gh`**，不访问任何 forge API，不要求 GitHub owner/repo，不创建/更新/关闭真实 PR。
- 所有新代码、状态、测试和文档只使用 `render-pr-content` / `prepare_pr_content` /
  `PR_CONTENT_READY`。

### 3.7 PolicyGateway 与 MCP 接口

- `PolicyGateway.create_draft_pr()` → 改名为 `prepare_pr_content()`。旧名保留为
  deprecated wrapper 调 `prepare_pr_content`，输出 DeprecationWarning，绝不访问网络。
- `_stage_draft_pr` → `_stage_pr_content`: 调用 `scripts/render-pr-content`（不再调
  `create-draft-pr` 的 `gh` 路径）。
- `_stage_windows_sync`: 调用 `win_sync` 前**重新加载并验证** `pr-content` artifact（见 §3.8）。
- 新增 `StateStore.inject_candidate(task_id, new_sha, from_state)`: 审计入口，仅供
  acceptance helper 调用（review-fix 受控 candidate 注入）。走合法 transition 路径，
  记录 `controlled_candidate_injection` 事件，清空 tested/reviewed_sha。生产
  PolicyGateway 不调用它。
- MCP server (`mcp/server.py`): 若暴露新 tool 需同步更新注册 + profile frontmatter +
  docs。当前 `prepare_pr_content`/`sync_windows` 仍为内部方法（非 MCP tool），除非需要
  Supervisor 直接调用。

### 3.8 Windows sync gate 改造

`SyncGates.draft_pr_created: bool` → `SyncGates.pr_content_ready: bool`。

`pr_content_ready` **不能是 bool 常量**。`check_gates()` 改为**实际校验** `pr-content`
artifact 包（不接受 bool 入参）:

- 读取 `<run-dir>/pr-content.sha256`、`pr-content.json`、`pr-content.md`。
- 重新计算 JSON/Markdown 的 SHA-256，与 `pr-content.sha256` 两行比对。
- 校验:
  - `pr-content.sha256` 存在且格式正确（两行）
  - JSON hash 一致
  - Markdown hash 一致
  - `schema_version` 正确
  - `task_id` == 当前 task
  - `workflow_state == "PR_CONTENT_READY"`
  - `base_branch` / `head_branch` 与配置一致
  - `candidate_sha` / `tested_sha` / `reviewed_sha` 与状态机记录一致且三相等
  - `review_decision == "APPROVED"`
  - `push.json` 中 `pushed_sha == candidate_sha` 且 `branch == 配置 task branch` 且
    `push_succeeded == true`
- 任一不一致 → `pr_content_ready=False` → `all_pass=False` → 拒绝 sync。

`PolicyGateway.sync_windows` / `_stage_windows_sync`:
- 调用 `win_sync` 前重新加载并验证上述全部字段（不传常量 True）。
- 校验失败 → `PolicyError("WINDOWS_SYNC_BLOCKED: pr-content artifact invalid: <detail>")`。

### 3.9 Acceptance 三场景重做

#### direct

新通过条件（全部满足）:
- `final_state == READY_FOR_HUMAN_REVIEW`
- `candidate == tested == reviewed`
- `pr-content.json` 有效（存在 + 可解析 + schema 字段齐全 + sha256 一致）
- `pr-content.md` 有效
- `pr-content.sha256` 有效
- **未执行任何 forge API**（`render-pr-content` 进程内无 `gh`/`requests`/`urllib` 调用；
  测试用 monkeypatch 断言无网络调用）

移除 `test_mode=False` 的 `gh pr create` 路径与 `is_real_pr` 断言。

#### review-fix

**主场景 PASS 必须同时满足**:

```text
protocol_passed == true
task_approved == true
final_state == READY_FOR_HUMAN_REVIEW
pr_content_valid == true
```

- `protocol_passed`: had_changes_requested and had_fix and had_incremental_review（保留）。
- `task_approved`: 终态 == `READY_FOR_HUMAN_REVIEW`。
- `pr_content_valid`: 同 direct 校验。

**Judge 正确进入 `NEEDS_HUMAN`**: 可作为**独立 safety 子场景**通过（记录为
`safety_behavior_evidence`），但**不能让主 review-fix 场景返回 PASS**（`task_approved=False`
→ 主场景 FAIL）。

**受控 candidate 注入**: 现有 `sqlite3 UPDATE tasks` 直写 (L451-456) 违反"不绕过状态机"。
改为通过 `StateStore.inject_candidate(task_id, new_sha, from_state=LOCAL_VERIFYING)`:
- 走合法 transition / 显式审计入口
- 记录 `controlled_candidate_injection` 事件
- 清空 tested/reviewed_sha
- acceptance helper 调用它而非裸 SQL

#### resume

完全重做，删除现有宽松断言。**不把 STALLED 作为 controller restart 后 reattach 的必要条件**。

**正确 mid-stage 中断恢复流程**:

1. Worker 已真实处于 RUNNING/PROCESSING。
2. Worker handle、stage attempt、owner lease 和 resume state 已写入 SQLite。
3. 终止 controller 进程（`terminate`，**不杀 Worker**）——模拟 controller crash。
4. 启动新的 PolicyGateway / WorkerMonitor，复用原 SQLite。
5. 等旧 owner lease 过期，**或**通过明确的安全接管协议获得 ownership。
6. 原 Worker 仍运行时直接 reattach。
7. 原 Worker 已完成时采集结果并完成原 attempt。
8. 只有 Worker 死亡、失联或长期无进展时才进入 STALLED/restart。

**至少测试 5 个子场景**:
- controller crash + Worker 仍运行 → reattach 成功
- controller crash + Worker 已完成 → 采集结果完成 attempt
- controller crash + Worker 已死亡 → STALLED/restart
- lease 未过期时不能抢占
- lease 过期后只能有一个新 owner 接管

**精确断言**（全部满足才 PASS）:
- `final_state == READY_FOR_HUMAN_REVIEW`
- 中断前 COMPLETED 的 stage，其 `attempt` 数**完全不增加**（`==`）
- 已消费的 Codex `call_index`/`total_used` **完全不增加**（`==`，删除 `>=`）
- 不产生重复 commit（candidate_sha 不回退；`git log origin/<task_branch>` 无重复）
- 不重复生成 PR 内容包（`pr-content.sha256` 与首次生成一致；StageStore `pr_content`
  stage attempt 不增加）
- 不重复 Windows sync（`windows_sync` stage attempt 不增加）
- `worker_id` / `terminal_id` / `owner_id` / `lease_until` / `resume_state` 在
  reattach 前后有完整证据链（记录到 evidence）

删除 `candidate_unchanged` tautology (L563-565) 与 `budget_not_respent` 的 `>=` (L548)。

### 3.10 Acceptance evidence append-only

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
  push.json
  artifact_manifest.json  # 各 artifact 路径 + sha256
```

- `<run-id>` = 时间戳 + scenario（如 `20260731-120000-direct`），**不可复用**，保证不覆盖。
- 每次 `run_scenario` 写入新 `<run-id>` 目录，**不覆盖**历史。
- `meta.scenarios[name]` 仍记录最新 result 指针（`evidence_path` + `passed`），但不删除
  旧 evidence。

**cleanup 改造**:
- 默认 cleanup 只清理: runtime DB、临时 worktree、task-owned 进程、`acc/` 测试分支。
- **保留** `acceptance/evidence/`。
- **删除所有** `_cleanup_acceptance_prs`（`gh pr list/close`）逻辑——不再执行任何
  PR list/close/label 操作。
- 保留 `_cleanup_acceptance_branches`（只删 `acc/` 前缀远程分支，安全）。
- 新增独立 `acceptance purge-evidence --force` 子命令（显式删除历史 evidence，需 `--force`）。

## 4. 涉及修改的文件

| 文件 | 改动 |
|------|------|
| `src/supervisor_cao/state/machine.py` | 新增 `PR_CONTENT_READY`、`PR_CONTENT_GENERATION_FAILED`；转移表；`migrate_legacy_state()`（惰性，非 get_task） |
| `src/supervisor_cao/mcp/policy_gateway.py` | `create_draft_pr`→`prepare_pr_content` deprecated wrapper；`_stage_draft_pr`→`_stage_pr_content` 调 `render-pr-content`；`sync_windows` 重新加载校验 pr-content；`inject_candidate()` 审计入口；push.json 写入（Executor stage） |
| `src/supervisor_cao/validation/windows_sync.py` | `SyncGates.draft_pr_created`→`pr_content_ready`；`check_gates` 实际校验 artifact 全字段 |
| `scripts/render-pr-content` | 新脚本（从 `create-draft-pr` 派生，移除所有 `gh` 调用，输出 PR 内容包 + stdout 契约；校验 push.json） |
| `scripts/create-draft-pr` | deprecated wrapper 调 `render-pr-content`，输出弃用提示，不调 gh/forge |
| `src/supervisor_cao/cli/acceptance.py` | 三场景重写；evidence append-only；cleanup 改造；移除 PR 清理；`inject_candidate` 替代裸 SQL |
| `src/supervisor_cao/cli/main.py` | 新增 `acceptance purge-evidence` 子命令 |
| `docs/ACCEPTANCE.md` | 重写三场景通过条件 + 状态描述 |
| `docs/WORKFLOW.md` | 状态机图更新 |
| `docs/USER_GUIDE.md` | PR handoff 说明（复制粘贴流程） |
| `README.md` | 终态说明 |
| `AGENTS.md` | 移除 Draft PR 门禁描述 |
| `tests/unit/test_state_machine.py` | 新转移、legacy 迁移、新任务不进 DRAFT_PR_CREATED |
| `tests/unit/test_policy_mcp_protocol.py` | tool 名更新 |
| `tests/unit/test_windows_sync.py` | `pr_content_ready` gate 校验全字段 |
| `tests/unit/test_pr_content.py` | **新增**: schema、校验、幂等、无网络、多 remote 类型、push.json 校验 |
| `tests/unit/test_acceptance_evidence.py` | **新增**: append-only、cleanup 保留、purge-evidence |
| `tests/unit/test_resume_reattach.py` | **新增**: 5 个 resume 子场景 |
| `tests/integration/test_workflow.py` | happy path 终态改名 |
| `tests/e2e/test_temp_repo_e2e.py` | PR 内容包生成 + stdout 契约 |

## 5. 测试矩阵（新增/修改）

| 测试 | 类型 | 断言 |
|------|------|------|
| APPROVED → PR_CONTENT_READY 合法 | unit | transition 成功 |
| 新任务不进入 DRAFT_PR_CREATED | unit | 无转移入口 |
| legacy DRAFT_PR_CREATED artifact 完整可迁移 | unit | 单事务迁移到 PR_CONTENT_READY + LEGACY_STATE_MIGRATED 事件 |
| legacy DRAFT_PR_CREATED artifact 不完整不静默回退 | unit | 保持原状态 + migration error 或 NEEDS_HUMAN |
| get_task 不触发迁移 | unit | DB 状态不变 |
| 缺任一 artifact 拒绝生成 | unit | exit 1 |
| 三 SHA 不同拒绝 | unit | exit 1 |
| review 非 APPROVED 拒绝 | unit | exit 1 |
| push.json 缺失/不一致拒绝 | unit | exit 1 |
| PR 内容生成不调网络 | unit | monkeypatch subprocess/socket 断言 |
| GitHub/GitCode/SSH remote 都能生成 | unit | 不依赖 remote 类型（只读 push.json） |
| rerun 幂等 | unit | 二次输出字节相同 |
| sha256 绑定 JSON+MD 两行格式 | unit | 格式正确 + hash 一致 |
| pr-content.json 无 generated_at/rendered_sha256 | unit | 字段不存在 |
| workflow_state == PR_CONTENT_READY | unit | 非 READY_FOR_HUMAN_REVIEW |
| title 不强制 [Draft] | unit | 无 [Draft] 前缀 |
| Windows sync 校验全字段 | unit | gate 随各字段有效性翻转 |
| cleanup 保留 evidence | unit | evidence 目录存在 |
| cleanup 不执行 PR list/close/label | unit | 无 gh 调用 |
| acceptance history 不覆盖 | unit | 两次 run 两个目录 |
| purge-evidence --force 删除 | unit | 删除后目录不存在 |
| direct 新通过条件 | e2e | 无 forge API + pr-content 有效 |
| review-fix 严格条件 | e2e | protocol+approved+final+pr-content |
| review-fix safety 子场景（Judge NEEDS_HUMAN） | e2e | safety 通过但主场景不 PASS |
| controller crash + Worker 仍运行 → reattach | e2e | 不重复 budget/stage/commit |
| controller crash + Worker 已完成 → 采集 | e2e | 完成 attempt |
| controller crash + Worker 已死亡 → STALLED | e2e | 正确进入 STALLED/restart |
| lease 未过期不能抢占 | e2e | 抢占失败 |
| lease 过期后单一新 owner | e2e | 只一个接管 |
| 无重复 budget/stage/commit/PR-content/sync | e2e | 严格 `==` 断言 |

## 6. 实施顺序（TDD，分逻辑 commit）

1. PR content schema、canonical renderer 和 checksum（`scripts/render-pr-content` + `test_pr_content.py`）
2. push evidence（`push.json` 写入 + 校验）
3. 状态机和 legacy migration（`machine.py` + `test_state_machine.py`）
4. PolicyGateway 与 MCP 接口（`prepare_pr_content` + `_stage_pr_content` + `inject_candidate`）
5. Windows sync gate（`windows_sync.py` + `test_windows_sync.py`）
6. append-only acceptance evidence（`acceptance.py` evidence + cleanup 改造 + `purge-evidence`）
7. direct 场景
8. review-fix 场景（含 `inject_candidate` + safety 子场景）
9. controller restart + Worker reattach 的真实 resume（5 子场景）
10. 文档和完整回归

每步: 先写测试（红）→ 实现（绿）→ lint/type → commit。

## 7. 验收

- 全部 unit/integration/e2e(temp-repo) 通过
- secret scan 无命中
- CLI smoke (`supervisor-cao --help`, `acceptance --help`) 正常
- live cao-server 运行 direct/review-fix/resume 三条，各产生 append-only evidence
- 三条都真实通过前，整体状态保持 `READY_WITH_KNOWN_LIMITATIONS`
- 最终回复输出可复制的 PR Title / Base / Head / Body + 三条 evidence 路径

## 8. 约束（实现过程中）

- 不自动创建任何平台 PR
- 不调用 forge API
- 不 merge
- 可以 push feature branch
- 不根据终端记忆补写验收成功

## 9. 已确认决策记录（用户 2026-07-31 批复）

1. **时间戳/checksum**: 不新增 `pr-content.meta.json`；`pr-content.{json,md}` 不含
   `generated_at`；`pr-content.sha256` 两行格式绑定 JSON+MD；删除 `rendered_sha256`；
   统一 UTF-8/LF/固定 key 顺序/末尾换行。
2. **状态/标题**: `workflow_state=PR_CONTENT_READY`；title 不强制 `[Draft]`。
3. **create-draft-pr**: deprecated wrapper，只调 render-pr-content，输出弃用提示，不调
   gh/forge，不要求 GitHub owner/repo。
4. **push evidence**: 新增 `push.json`，renderer 只本地校验，不访问网络。
5. **resume**: 不以 STALLED 为必要条件；8 步流程 + 5 子场景。
6. **legacy**: 惰性迁移只在 resume/advance/migration；get_task 不改 DB；artifact 不完整
   不静默回退。
7. **Windows sync**: pr_content_ready 非常量；sync 前重新加载验证全字段。
8. **review-fix**: 主场景四条件；Judge NEEDS_HUMAN 是独立 safety 子场景。
9. **evidence**: append-only，run ID 不可复用；cleanup 保留 evidence，不执行 PR 清理；
   purge-evidence --force。
10. **实施顺序**: 10 步。
