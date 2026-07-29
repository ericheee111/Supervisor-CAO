[English](#acceptance-criteria-and-results) | [简体中文](#验收标准与结果)

# Acceptance Criteria and Results

Run from the repo root on WSL2 Ubuntu-24.04: `python -m pytest tests/ -q`.

## Test suite (all passing)

| Level | Count | Scope |
|-------|-------|-------|
| Unit | 51 | state machine, budget, schema, SHA, locks, windows-dirty, ff, PR body, secret scan, config, permissions |
| Integration | 10 | planner, executor-fix, verifier-fail, stale, budget, pool, windows-blocked, happy-path |
| E2E | 13 | temporary-repository full flow |
| Stability | 10/10 | short callback x10, one long worker, one timeout, one callback-recovery |

## Unit tests (51)

Cover every deterministic enforcement path:

- **State machine**: legal forward transitions only (no skipping);
  `tested_sha == candidate_sha`; `reviewed_sha == tested_sha`; new candidate
  invalidates tested/reviewed; gate checks before `LOCAL_VERIFIED`,
  `REMOTE_VERIFIED`, `APPROVED`, `DRAFT_PR_CREATED`; error states reachable
  from any non-terminal state; full audit log.
- **Budget**: per-task per-role Codex cap; atomic spend under lock;
  `CODEX_BUDGET_EXHAUSTED` on overflow; persisted call log.
- **Schema**: `task`/`plan`/`implementation`/`verification`/`review`/`decision`.
- **SHA / locks / windows-dirty / fast-forward / PR body / secret scan /
  config / permissions**: each enforced and tested.

## Integration tests (10)

`planner`, `executor-fix`, `verifier-fail`, `stale`, `budget`, `pool`,
`windows-blocked`, `happy-path`, plus report-compression and
dispute-arbitration. No live destructive tests against the real project repo;
the real-project integration test is read-only unless a human explicitly starts
a real task.

## E2E (13)

Temporary-repository full flow:

```
Supervisor -> Codex Planner -> GLM Executor -> Qwen Verifier
-> Codex Reviewer -> fix cycle -> re-verification -> incremental review
-> Draft PR path -> protected sync path
```

## Stability (10/10)

Short callback flow repeated 10x (no drift, no leaked state); one long-running
worker; one timeout; one callback-recovery. Known CAO/OpenCode limitations
documented honestly (below).

## Pandas read-only smoke

Confirms without modifying anything: config loads, base branch (`dev`)
reachable, local repos inspectable, remote slots health-checked.

Result: **3 PASS** (config load, base branch reachable, local repo inspectable)
+ **1 LIMITATION** (remote pool/containers/conda over SSH — see known
limitations).

## Supervisor benchmark

`scripts/supervisor-benchmark` exercises the Supervisor role with both cheap
providers against canned tasks.

- **GLM (primary Supervisor)**: 4/4.
- **Qwen (backup Supervisor)**: 4/4.
- **Qwen as primary** (per model map): 4/4.

Both providers are viable Supervisors; GLM is the configured primary, Qwen the
configured backup.

## Known limitations

These do not block the core workflow; documented honestly per the stability
criteria.

1. **Remote SSH not configured.** The remote validation pool (containers, conda
   env) over SSH is a `LIMITATION`. Remote-pool acquire/release and restoration
   are covered by unit/integration fixtures; the live remote path is not
   exercised end-to-end because SSH to the validation host is not configured in
   this environment. Status for remote-pool-dependent tasks:
   `READY_WITH_KNOWN_LIMITATIONS`.
2. **WSL2 network restricted.** A fake-ip VPN hijacks DNS. The offline
   wheelhouse install path (`docs/INSTALL.md`) and DoH + `/etc/hosts` mitigation
   (`docs/TROUBLESHOOTING.md`) are used; CAO and providers were installed
   offline.
3. **Codex CLI on Windows path.** `codex` is not on the WSL PATH by default.
   Set `CODEX_BIN` to the absolute path (WSL or `/mnt/c/...`); `doctor` honors
   it.
4. **CAO OpenCode provider experimental.** Multi-agent callback uses inbox
   polling fallback (CAO issues #203/#115); long-task delivery and callback
   recovery are tested separately, not via the live callback path.

## Security acceptance

- `scripts/scan-secrets` passes on all tracked files.
- Private files (`*.local.yaml`, `models.local.yaml`, `secrets.env`,
  `*.private.md`, `auth.json`) are git-ignored.
- No private identifiers (internal hosts, container names, usernames, private
  paths) in tracked files.

## Final status

- `READY` — all mandatory checks pass.
- `READY_WITH_KNOWN_LIMITATIONS` — core workflow works; documented non-critical
  limitation remains (remote SSH pool).
- `BLOCKED` — a mandatory capability cannot be completed.

Current overall: **READY_WITH_KNOWN_LIMITATIONS** (remote SSH pool not
configured live; all local, unit, integration, E2E, and stability tests pass).

## See also

`docs/USER_GUIDE.md`, `docs/TROUBLESHOOTING.md`, `docs/SECURITY.md`.

---

# 验收标准与结果

在 WSL2 Ubuntu-24.04 上从仓库根目录运行：`python -m pytest tests/ -q`。

## 测试套件（全部通过）

| 级别 | 数量 | 范围 |
|-------|-------|-------|
| 单元 | 51 | 状态机、预算、schema、SHA、锁、windows-dirty、ff、PR body、密钥扫描、配置、权限 |
| 集成 | 10 | planner、executor-fix、verifier-fail、stale、budget、pool、windows-blocked、happy-path |
| E2E | 13 | 临时仓库完整流程 |
| 稳定性 | 10/10 | 短回调 x10、一个长 worker、一个超时、一个回调恢复 |

## 单元测试（51）

覆盖每一条确定性强制执行路径：

- **状态机**：仅允许合法的前向转换（不可跳过）；`tested_sha == candidate_sha`；`reviewed_sha == tested_sha`；新候选使 tested/reviewed 失效；在 `LOCAL_VERIFIED`、`REMOTE_VERIFIED`、`APPROVED`、`DRAFT_PR_CREATED` 之前进行门禁检查；错误状态可从任何非终止状态到达；完整审计日志。
- **预算**：每任务每角色的 Codex 上限；锁下原子化消费；溢出时 `CODEX_BUDGET_EXHAUSTED`；持久化调用日志。
- **Schema**：`task`/`plan`/`implementation`/`verification`/`review`/`decision`。
- **SHA / 锁 / windows-dirty / fast-forward / PR body / 密钥扫描 / 配置 / 权限**：每项都被强制执行并测试。

## 集成测试（10）

`planner`、`executor-fix`、`verifier-fail`、stale、budget、pool、`windows-blocked`、`happy-path`，外加 report-compression 和 dispute-arbitration。不对真实项目仓库做实时破坏性测试；真实项目集成测试是只读的，除非人工显式启动真实任务。

## E2E（13）

临时仓库完整流程：

```
Supervisor -> Codex Planner -> GLM Executor -> Qwen Verifier
-> Codex Reviewer -> fix cycle -> re-verification -> incremental review
-> Draft PR path -> protected sync path
```

## 稳定性（10/10）

短回调流程重复 10 次（无漂移、无泄漏状态）；一个长时间运行的 worker；一个超时；一个回调恢复。已知的 CAO/OpenCode 局限性已如实记录（见下文）。

## Pandas 只读 smoke test

在不动任何内容的情况下确认：配置加载、base 分支（`dev`）可达、本地仓库可检查、远程槽位健康检查通过。

结果：**3 PASS**（配置加载、base 分支可达、本地仓库可检查）+ **1 LIMITATION**（通过 SSH 的远程池/容器/conda — 见已知局限性）。

## Supervisor 基准测试

`scripts/supervisor-benchmark` 使用两个廉价 provider 对 canned 任务执行 Supervisor 角色。

- **GLM（主 Supervisor）**：4/4。
- **Qwen（备用 Supervisor）**：4/4。
- **Qwen 作为主 Supervisor**（按 model map）：4/4。

两个 provider 都可作为 Supervisor；GLM 是配置的主，Qwen 是配置的备。

## 已知局限性

这些不阻塞核心工作流；按稳定性标准如实记录。

1. **远程 SSH 未配置。** 通过 SSH 的远程验证池（容器、conda 环境）是一个 `LIMITATION`。远程池的 acquire/release 与恢复由单元/集成 fixtures 覆盖；实时远程路径未端到端执行，因为此环境中未配置到验证主机的 SSH。依赖远程池任务的状态：`READY_WITH_KNOWN_LIMITATIONS`。
2. **WSL2 网络受限。** 一个 fake-ip VPN 劫持了 DNS。使用离线 wheelhouse 安装路径（`docs/INSTALL.md`）以及 DoH + `/etc/hosts` 缓解措施（`docs/TROUBLESHOOTING.md`）；CAO 和各 provider 均离线安装。
3. **Codex CLI 在 Windows 路径上。** `codex` 默认不在 WSL PATH 上。将 `CODEX_BIN` 设为绝对路径（WSL 或 `/mnt/c/...`）；`doctor` 会遵循它。
4. **CAO OpenCode provider 为实验性。** 多 agent 回调使用 inbox 轮询回退（CAO issue #203/#115）；长任务交付与回调恢复被分别测试，而非通过实时回调路径。

## 安全验收

- `scripts/scan-secrets` 在所有受跟踪文件上通过。
- 私有文件（`*.local.yaml`、`models.local.yaml`、`secrets.env`、`*.private.md`、`auth.json`）被 git-ignored。
- 受跟踪文件中无私有标识符（内部主机名、容器名、用户名、私有路径）。

## 最终状态

- `READY` — 所有强制检查通过。
- `READY_WITH_KNOWN_LIMITATIONS` — 核心工作流可用；存在已记录的非关键性局限（远程 SSH 池）。
- `BLOCKED` — 某项强制能力无法完成。

当前总体：**READY_WITH_KNOWN_LIMITATIONS**（远程 SSH 池未配置实时；所有本地、单元、集成、E2E 与稳定性测试通过）。

## 另请参阅

`docs/USER_GUIDE.md`、`docs/TROUBLESHOOTING.md`、`docs/SECURITY.md`。
