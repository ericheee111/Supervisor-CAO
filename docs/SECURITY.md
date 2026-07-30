[English](#security) | [简体中文](#安全)

# Security

Supervisor-CAO coordinates external model providers, local CLI tools, Git
repositories, remote validation hosts, and Windows/WSL filesystems. Every
boundary is treated as potentially unsafe.

## Secrets isolation

**Never committed to the public repository:**

- API keys, tokens, cookies
- Codex/OpenCode authentication files (`auth.json`, `config.toml`, `opencode.jsonc`)
- Internal SSH host aliases
- Docker container names
- Internal usernames
- Private Windows/WSL paths
- Complete remote logs
- `models.local.yaml`, `*.local.yaml`, `secrets.env`, `.env`
- Private requirement/design documents (`*.private.md`)

**Where real data lives (local only, outside Git):**

```
~/.config/supervisor-cao/          # models.local.yaml, projects/*.local.yaml, secrets.env
~/.local/state/supervisor-cao/     # task DB, codex budget DB
~/cao-runs/                        # logs, test results, verification artifacts, audit records
```

### Secret scanner

`scripts/scan-secrets` runs before every push. It detects:
- Common API key formats (OpenAI `sk-`, AWS `AKIA`, GitHub `ghp_`, Google `AIza`, Bearer tokens)
- Known private identifiers (internal host aliases, container names, usernames)
- Private path leaks
- Forbidden files (`.env`, `secrets.env`, `*.local.yaml`, `*.private.md`, `auth.json`)

Exit 1 blocks the push. The scanner skips itself (it intentionally lists
identifiers to detect) and common cache/build directories.

## Git safety

**Forbidden in all automated workflows:**

```
git reset --hard
git clean -fdx
git push --force
automatic merge
overwriting dirty worktrees
cherry-pick to current branch
auto-merge of dev
```

**Allowed:**
- Task branches (`agent/<task-id>`) may be committed and pushed
- Fast-forward only sync to Windows repo
- Base branches (`dev`) are never rewritten

**SHA binding (enforced in code):**
- `tested_sha` must equal `candidate_sha`
- `reviewed_sha` must equal `tested_sha`
- Any new commit invalidates prior verification and review
- Natural-language "passed" cannot replace artifacts and exit codes

## Role isolation

| Role | Source | Git | Remote | Codex |
|------|--------|-----|--------|-------|
| Supervisor | read task state | none | none | none |
| Researcher | read-only | read-only | none | none |
| Codex Planner | read-only | read-only | none | 1 planner |
| GLM Executor | own worktree only | commit+push task branch | none | none |
| Qwen Verifier | read-only | none | via scripts only | none |
| Codex Reviewer | read-only | read-only | none | 1 review |
| Codex Judge | read-only | read-only | none | 1 judge |

Only the platform sync script may operate on the Windows repository.

## Remote validation safety

- Atomic lock before changing remote repo state (one task per container)
- Record original branch, HEAD, and cleanliness before
- Refuse dirty repositories (`REMOTE_WORKTREE_DIRTY`)
- Restore original branch and HEAD after (no `reset --hard`, no `clean -fdx`)
- Validate restoration; mark `UNHEALTHY` on failure
- LLMs never poll long-running verification; deterministic runners execute and collect

## Codex budget enforcement

The budget is enforced in code (`src/supervisor_cao/budget/codex.py`), not by
the Supervisor. On exhaustion (`CODEX_BUDGET_EXHAUSTED`), the task stops and
requires human intervention. The Supervisor cannot self-track or bypass it.

Codex is never used for: polling, log formatting, fixed-threshold calculations,
lint, ordinary retries, status routing, or message forwarding.

## CAO provider notes

The OpenCode provider is experimental. Multi-agent callback uses inbox polling
fallback (CAO issues #203/#115). CAO isolates its OpenCode config at
`~/.aws/opencode/`, separate from the user's personal `~/.config/opencode/`.
Long-task message delivery and recovery are tested separately.

## Windows sync gates

All 7 gates must pass before sync:
1. candidate pushed to remote
2. `tested_sha == candidate_sha`
3. `reviewed_sha == candidate_sha`
4. Review APPROVED
5. Draft PR created
6. Windows worktree clean
7. Local task branch fast-forwardable

Final verification: `Windows HEAD == candidate SHA`. Failure → `WINDOWS_SYNC_BLOCKED`.

## Review dispute safety

No free-form group chat. Maximum sequence:
```
Reviewer finding → Executor response (1) → Reviewer rebuttal (1) → Judge (1)
```
No new evidence = no further round. Each finding needs: ID, severity, category,
file, line, claim, failure scenario, evidence, recommended direction.

---

# 安全

Supervisor-CAO 协调外部模型提供商、本地 CLI 工具、Git
仓库、远程验证主机以及 Windows/WSL 文件系统。每一个边界都被视为潜在不安全。

## 密钥隔离

**绝不提交到公共仓库：**

- API key、token、cookie
- Codex/OpenCode 认证文件（`auth.json`、`config.toml`、`opencode.jsonc`）
- 内部 SSH 主机别名
- Docker 容器名
- 内部用户名
- 私有 Windows/WSL 路径
- 完整的远程日志
- `models.local.yaml`、`*.local.yaml`、`secrets.env`、`.env`
- 私有需求/设计文档（`*.private.md`）

**真实数据存放位置（仅本地，位于 Git 之外）：**

```
~/.config/supervisor-cao/          # models.local.yaml, projects/*.local.yaml, secrets.env
~/.local/state/supervisor-cao/     # task DB, codex budget DB
~/cao-runs/                        # logs, test results, verification artifacts, audit records
```

### Secret scanner

`scripts/scan-secrets` 在每次 push 之前运行。它检测：
- 常见 API key 格式（OpenAI `sk-`、AWS `AKIA`、GitHub `ghp_`、Google `AIza`、Bearer token）
- 已知私有标识符（内部主机别名、容器名、用户名）
- 私有路径泄漏
- 禁止文件（`.env`、`secrets.env`、`*.local.yaml`、`*.private.md`、`auth.json`）

退出码 1 会阻止 push。扫描器跳过自身（它有意列出要检测的标识符）以及常见
缓存/构建目录。

## Git 安全

**在所有自动化工作流中禁止：**

```
git reset --hard
git clean -fdx
git push --force
automatic merge
overwriting dirty worktrees
cherry-pick to current branch
auto-merge of dev
```

**允许：**
- 任务分支（`agent/<task-id>`）可以被提交和 push
- 仅 fast-forward 同步到 Windows 仓库
- 基础分支（`dev`）永不重写

**SHA 绑定（在代码中强制）：**
- `tested_sha` 必须等于 `candidate_sha`
- `reviewed_sha` 必须等于 `tested_sha`
- 任何新提交都会使先前的验证和评审失效
- 自然语言的 "passed" 不能替代工件和退出码

## 角色隔离

| 角色 | 来源 | Git | 远程 | Codex |
|------|--------|-----|--------|-------|
| Supervisor | 读取任务状态 | 无 | 无 | 无 |
| Researcher | 只读 | 只读 | 无 | 无 |
| Codex Planner | 只读 | 只读 | 无 | 1 planner |
| GLM Executor | 仅自身 worktree | commit+push 任务分支 | 无 | 无 |
| Qwen Verifier | 只读 | 无 | 仅通过脚本 | 无 |
| Codex Reviewer | 只读 | 只读 | 无 | 1 review |
| Codex Judge | 只读 | 只读 | 无 | 1 judge |

只有平台同步脚本可以操作 Windows 仓库。

## 远程验证安全

- 在更改远程仓库状态之前加原子锁（每个容器一个任务）
- 在操作前记录原始分支、HEAD 和干净状态
- 拒绝脏仓库（`REMOTE_WORKTREE_DIRTY`）
- 操作后恢复原始分支和 HEAD（不使用 `reset --hard`，不使用 `clean -fdx`）
- 验证恢复；失败时标记为 `UNHEALTHY`
- LLM 永不轮询长时间运行的验证；由确定性 runner 执行并收集

## Codex 预算强制

预算在代码中强制（`src/supervisor_cao/budget/codex.py`），而非由
Supervisor 强制。在耗尽时（`CODEX_BUDGET_EXHAUSTED`），任务停止并
需要人工介入。Supervisor 无法自追踪或绕过它。

Codex 永不用于：轮询、日志格式化、固定阈值计算、
lint、普通重试、状态路由或消息转发。

## CAO 提供商说明

OpenCode 提供商是实验性的。多智能体回调使用 inbox 轮询
回退（CAO issues #203/#115）。CAO 将其 OpenCode 配置隔离在
`~/.aws/opencode/`，与用户个人 `~/.config/opencode/` 分离。
长任务消息投递和恢复单独测试。

## Windows 同步门禁

所有 7 个门禁必须在同步前通过：
1. candidate 已 push 到远程
2. `tested_sha == candidate_sha`
3. `reviewed_sha == candidate_sha`
4. 评审 APPROVED
5. 已创建 Draft PR
6. Windows worktree 干净
7. 本地任务分支可 fast-forward

最终验证：`Windows HEAD == candidate SHA`。失败 → `WINDOWS_SYNC_BLOCKED`。

## 评审争议安全

不允许自由形式的群聊。最大序列：
```
Reviewer finding → Executor response (1) → Reviewer rebuttal (1) → Judge (1)
```
没有新证据 = 没有进一步轮次。每条 finding 需要：ID、严重度、类别、
文件、行、claim、失败场景、证据、建议方向。
