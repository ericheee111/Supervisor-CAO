[English](#acceptance-criteria-and-results) | [简体中文](#验收标准与结果)

# Acceptance Criteria and Results

Run from the repo root on WSL2 Ubuntu-24.04: `python -m pytest tests/ -q`.

## Current status: BLOCKED

Per the active goal, the overall status stays **BLOCKED** until generalization
is complete and CI is green on GitHub Actions (not just locally).

The generic, project-agnostic state of the platform is implemented and locally
verified: the policy MCP architecture, state machine, budget, schema validation,
idempotent resume, remote pipefail fix, and draft-PR artifact gate are all in
place and unit-tested. The generic temp-repository E2E
(`tests/e2e/test_temp_repo_e2e.py`) drives the full deterministic policy flow on
a throwaway git repo with mocked workers and passes. Unit and integration tests
pass locally. Remaining work: the real GitHub Actions CI run must be green
end-to-end (it currently defines the steps below; this doc tracks that it must
pass on the remote, not only locally).

## Test suite

| Level | Scope |
|-------|-------|
| Unit | state machine, budget, schema, SHA, locks, windows-dirty, ff, PR body, secret scan, config, config-safety, permissions, model resolver, worker runner (strict JSON extraction), stage resume (idempotency), policy MCP protocol, remote pipefail |
| Integration | planner, executor-fix, verifier-fail, stale, budget, pool, windows-blocked, happy-path |
| Temp-repo E2E | `tests/e2e/test_temp_repo_e2e.py` — full deterministic policy flow on a temporary git repo with mocked workers |
| Secret scan | `scripts/scan-secrets` over tracked files (private identifiers read from `~/.config/supervisor-cao/private-identifiers.txt`) |
| CI | GitHub Actions: install dependencies + unit + integration + temp-repo E2E + secret scan + CLI import smoke |

Run locally from the repo root: `python -m pytest tests/ -q` (unit + integration)
and `PYTHONPATH=src python tests/e2e/test_temp_repo_e2e.py` (temp-repo E2E).

## Unit tests

Cover every deterministic enforcement path:

- **State machine**: legal forward transitions only (no skipping);
  `tested_sha == candidate_sha`; `reviewed_sha == tested_sha`; new candidate
  invalidates tested/reviewed; gate checks before `LOCAL_VERIFIED`,
  `REMOTE_VERIFIED`, `APPROVED`, `DRAFT_PR_CREATED`; error states reachable
  from any non-terminal state; full audit log.
- **Budget**: per-task per-role Codex cap; atomic spend via `BEGIN IMMEDIATE`
  (cross-process safe); `CODEX_BUDGET_EXHAUSTED` on overflow; persisted log.
- **Config safety**: task overrides may only touch test/benchmark/acceptance
  fields; repo paths, SSH, containers, base_branch, codex_budget,
  executor_limits are forbidden in task overrides.
- **Schema**: `task`/`plan`/`implementation`/`verification`/`review`/`decision`.
- **SHA / locks / windows-dirty / fast-forward / PR body / secret scan /
  model resolver**: each enforced and tested.

## Integration tests

`planner`, `executor-fix`, `verifier-fail`, `stale`, `budget`, `pool`,
`windows-blocked`, `happy-path`. These run against fixtures and temp repos —
no live destructive tests against any real project repository.

## Temp-repo E2E

`tests/e2e/test_temp_repo_e2e.py` — full deterministic policy flow on a
temporary git repo with mocked worker results:

```
Supervisor -> Codex Planner -> GLM Executor -> Qwen Verifier
-> Codex Reviewer -> fix cycle -> re-verification -> incremental review
-> Draft PR path -> protected sync path
```

This is the generic, project-agnostic E2E. It creates a throwaway git repo,
exercises the state machine end-to-end, and validates worktree create + commit
+ push, Codex budget, the Windows-sync gate (blocked when dirty, passes when
clean), and Draft-PR body generation. It does not call real LLM/Codex agents.

## Supervisor benchmark

`scripts/supervisor-benchmark` exercises the Supervisor role on a canned task
against the configured providers (minimal conversation, JSON output, SHA
fidelity, gate awareness). Provider/model IDs come from
`~/.config/supervisor-cao/models.local.yaml` (produced by `scripts/detect-models`);
no model IDs are hardcoded in the repo.

## Policy gateway

`src/supervisor_cao/mcp/policy_gateway.py` — the Supervisor has no arbitrary
bash. It can only call: `create_task`, `advance_task`, `call_planner`,
`start_executor`, `run_verification`, `call_reviewer`, `call_judge`,
`create_draft_pr`, `sync_windows`. Each enforces state machine, budget, SHA,
worktree, and gates in code.

## Remote verification pool

`scripts/run-verification` — single try/finally transaction: acquire owner
lock → check clean → record git state → checkout → install → run the
configured verification command → restore → release (same owner only). The
verification command is generic and configurable via
`scripts/run-verification --verify-command` (repeatable) or `--verify-script`;
the core only reads exit codes, logs, SHAs, and structured results. Stale lock
detection (2h). Restore failure keeps lock + marks UNHEALTHY. Never
`reset --hard` or `clean -fdx`.

## Security acceptance

- `scripts/scan-secrets` passes on all tracked files. Private identifiers are
  read from `~/.config/supervisor-cao/private-identifiers.txt` (private, never
  committed).
- Private files (`*.local.yaml`, `models.local.yaml`, `secrets.env`,
  `*.private.md`, `auth.json`) are git-ignored.
- No private identifiers (real hosts, container names, usernames, paths) in
  tracked files.
- `.gitignore` no longer ignores `src/supervisor_cao/state/` (critical fix).

## Known limitations

1. **CAO OpenCode provider experimental.** Multi-agent callback uses inbox
   polling fallback (CAO issues #203/#115). The generic temp-repo E2E drives
   the deterministic policy layer with mocked workers rather than live
   `cao launch` multi-agent tmux sessions. Full `handoff`/`assign`/
   `send_message` multi-agent testing requires a live `cao-server` + multiple
   worker sessions in tmux and is out of scope for the generic E2E.
2. **Live-LLM E2E not in CI.** `tests/e2e/test_live_cao_e2e.py` exercises the
   policy gateway with real provider calls; it requires configured providers
   and a live `cao-server`, so it is not part of the generic CI matrix. The
   generic, repeatable E2E is `tests/e2e/test_temp_repo_e2e.py`.

## Final status

- `READY` — all mandatory checks pass (including green GitHub Actions CI).
- `READY_WITH_KNOWN_LIMITATIONS` — core workflow works; a documented
  non-critical limitation remains.
- `BLOCKED` — a mandatory capability cannot be completed.

Current overall: **BLOCKED** — generalization is complete at the code/doc
level and all local checks (unit, integration, temp-repo E2E, secret scan)
pass, but real GitHub Actions CI must be confirmed green before moving to
`READY`/`READY_WITH_KNOWN_LIMITATIONS`.

## See also

`docs/USER_GUIDE.md`, `docs/TROUBLESHOOTING.md`, `docs/SECURITY.md`.

---

# 验收标准与结果

在 WSL2 Ubuntu-24.04 上从仓库根目录运行：`python -m pytest tests/ -q`。

## 当前状态：BLOCKED

按当前目标，在通用化完成且 GitHub Actions CI 变绿（不仅是本地）之前，总体状态保持
**BLOCKED**。

平台的通用、与项目无关状态已实现并在本地验证：policy MCP 架构、状态机、预算、schema
校验、幂等恢复、远程 pipefail 修复以及 draft-PR 产物门禁均已就绪并通过单元测试。通用的
临时仓库 E2E（`tests/e2e/test_temp_repo_e2e.py`）在一个一次性 git 仓库上以 mocked worker
驱动完整的确定性策略流程并通过。单元与集成测试在本地通过。剩余工作：真实的 GitHub Actions
CI 运行必须端到端变绿（它当前定义了下列步骤；本文档跟踪它必须在远端通过，而不仅是本地）。

## 测试套件

| 级别 | 范围 |
|-------|-------|
| 单元 | 状态机、预算、schema、SHA、锁、windows-dirty、ff、PR body、密钥扫描、配置、配置安全、权限、model resolver、worker runner（严格 JSON 抽取）、stage resume（幂等）、policy MCP 协议、remote pipefail |
| 集成 | planner、executor-fix、verifier-fail、stale、budget、pool、windows-blocked、happy-path |
| 临时仓库 E2E | `tests/e2e/test_temp_repo_e2e.py` — 在临时 git 仓库上以 mocked worker 驱动完整确定性策略流程 |
| 密钥扫描 | `scripts/scan-secrets` 扫描受跟踪文件（私有标识符读取自 `~/.config/supervisor-cao/private-identifiers.txt`） |
| CI | GitHub Actions：安装依赖 + 单元 + 集成 + 临时仓库 E2E + 密钥扫描 + CLI 导入冒烟 |

本地从仓库根目录运行：`python -m pytest tests/ -q`（单元 + 集成）以及
`PYTHONPATH=src python tests/e2e/test_temp_repo_e2e.py`（临时仓库 E2E）。

## 单元测试

覆盖每一条确定性强制执行路径：

- **状态机**：仅允许合法的前向转换（不可跳过）；`tested_sha == candidate_sha`；`reviewed_sha == tested_sha`；新候选使 tested/reviewed 失效；在 `LOCAL_VERIFIED`、`REMOTE_VERIFIED`、`APPROVED`、`DRAFT_PR_CREATED` 之前进行门禁检查；错误状态可从任何非终止状态到达；完整审计日志。
- **预算**：每任务每角色的 Codex 上限；通过 `BEGIN IMMEDIATE` 原子化消费（跨进程安全）；溢出时 `CODEX_BUDGET_EXHAUSTED`；持久化调用日志。
- **配置安全**：任务覆盖只允许触碰 test/benchmark/acceptance 字段；repo 路径、SSH、containers、base_branch、codex_budget、executor_limits 在任务覆盖中被禁止。
- **Schema**：`task`/`plan`/`implementation`/`verification`/`review`/`decision`。
- **SHA / 锁 / windows-dirty / fast-forward / PR body / 密钥扫描 / model resolver**：每项都被强制执行并测试。

## 集成测试

`planner`、`executor-fix`、`verifier-fail`、stale、budget、pool、`windows-blocked`、`happy-path`。这些针对 fixtures 和临时仓库运行 — 不对任何真实项目仓库做实时破坏性测试。

## 临时仓库 E2E

`tests/e2e/test_temp_repo_e2e.py` — 在临时 git 仓库上以 mocked worker 结果驱动完整确定性策略流程：

```
Supervisor -> Codex Planner -> GLM Executor -> Qwen Verifier
-> Codex Reviewer -> fix cycle -> re-verification -> incremental review
-> Draft PR path -> protected sync path
```

这是通用的、与项目无关的 E2E。它创建一个一次性 git 仓库，端到端驱动状态机，并验证
worktree 创建 + commit + push、Codex 预算、Windows 同步门禁（脏时阻塞，干净时通过）以及
Draft-PR body 生成。它不调用真实 LLM/Codex agent。

## Supervisor 基准测试

`scripts/supervisor-benchmark` 对 canned 任务执行 Supervisor 角色（最小化对话、JSON 输出、
SHA 保真、门禁感知）。Provider/model ID 来自 `~/.config/supervisor-cao/models.local.yaml`
（由 `scripts/detect-models` 生成）；仓库中不硬编码任何 model ID。

## Policy gateway

`src/supervisor_cao/mcp/policy_gateway.py` — Supervisor 没有任意 bash。它只能调用：
`create_task`、`advance_task`、`call_planner`、`start_executor`、`run_verification`、
`call_reviewer`、`call_judge`、`create_draft_pr`、`sync_windows`。每一项都在代码中强制执行
状态机、预算、SHA、worktree 与门禁。

## 远程验证池

`scripts/run-verification` — 单次 try/finally 事务：获取 owner 锁 → 检查干净 → 记录 git 状态
→ checkout → install → 运行配置的验证命令 → 恢复 → 释放（仅同一 owner）。验证命令是通用的，
可通过 `scripts/run-verification --verify-command`（可重复）或 `--verify-script` 配置；核心
只读取退出码、日志、SHA 与结构化结果。陈旧锁检测（2 小时）。恢复失败保留锁并标记 UNHEALTHY。
绝不 `reset --hard` 或 `clean -fdx`。

## 安全验收

- `scripts/scan-secrets` 在所有受跟踪文件上通过。私有标识符读取自
  `~/.config/supervisor-cao/private-identifiers.txt`（私有，绝不提交）。
- 私有文件（`*.local.yaml`、`models.local.yaml`、`secrets.env`、`*.private.md`、`auth.json`）被 git-ignored。
- 受跟踪文件中无私有标识符（真实主机名、容器名、用户名、路径）。
- `.gitignore` 不再忽略 `src/supervisor_cao/state/`（关键修复）。

## 已知局限性

1. **CAO OpenCode provider 为实验性。** 多 agent 回调使用 inbox 轮询回退（CAO issue #203/#115）。通用临时仓库 E2E 以 mocked worker 驱动确定性策略层，而非实时 `cao launch` 多 agent tmux 会话。完整的 `handoff`/`assign`/`send_message` 多 agent 测试需要实时 `cao-server` + 多个 tmux worker 会话，不在通用 E2E 范围内。
2. **实时 LLM E2E 不在 CI 中。** `tests/e2e/test_live_cao_e2e.py` 以真实 provider 调用驱动 policy gateway；它要求已配置的 provider 和实时 `cao-server`，因此不属于通用 CI 矩阵。通用、可重复的 E2E 是 `tests/e2e/test_temp_repo_e2e.py`。

## 最终状态

- `READY` — 所有强制检查通过（包括 GitHub Actions CI 变绿）。
- `READY_WITH_KNOWN_LIMITATIONS` — 核心工作流可用；存在已记录的非关键性局限。
- `BLOCKED` — 某项强制能力无法完成。

当前总体：**BLOCKED** — 通用化在代码/文档层面已完成，所有本地检查（单元、集成、临时仓库 E2E、
密钥扫描）通过，但在转向 `READY`/`READY_WITH_KNOWN_LIMITATIONS` 之前，必须确认真实的 GitHub
Actions CI 变绿。

## 另请参阅

`docs/USER_GUIDE.md`、`docs/TROUBLESHOOTING.md`、`docs/SECURITY.md`。
