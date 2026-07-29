[English](#troubleshooting) | [简体中文](#故障排查)

# Troubleshooting

## cao-server not starting

`supervisor-cao up` prints `FAILED`; `doctor` shows `cao-server down`.

```bash
pgrep -af cao-server; ss -ltnp 2>/dev/null | grep 9889   # running? port held?
cat /tmp/cao-server.log; tmux ls     # errors + leftover sessions
supervisor-cao down && pkill -f cao-server 2>/dev/null && supervisor-cao up
curl -s http://127.0.0.1:9889/health # expect 200
```

## OpenCode provider errors

Supervisor can't reach GLM/Qwen; `detect-models` reports unconfigured roles.

```bash
ls -l ~/.local/share/opencode/auth.json    # auth present? (or $OPENCODE_AUTH_PATH)
opencode models                            # provider/model IDs detected?
python3 scripts/detect-models --check      # exit 2 if a role unconfigured
```

`detect-models` never prints API keys. A role showing `configured: false` means
the `candidates` list in `scripts/detect-models` doesn't match `opencode
models`; extend it or authenticate the missing provider. CAO isolates OpenCode
config at `~/.aws/opencode/` (separate from `~/.config/opencode/`).

## Codex CLI not found

`doctor` shows `Codex CLI MISSING`; Codex planner/review calls fail. Set
`CODEX_BIN` to an absolute path (WSL-side or `/mnt/c/...`) and re-run `doctor`:

```bash
export CODEX_BIN="/mnt/c/Users/<USER>/AppData/Local/bin/codex.exe"
supervisor-cao doctor    # Codex CLI should now be ok
```

## WSL2 network / DNS issues

`uv tool install`/`git clone`/`pip download` fail with DNS errors; a fake-ip
VPN hijacks resolution. Mitigation A — DoH + `/etc/hosts` (online but
DNS-broken): `sudo python3 .doh_hosts_final.py`, then
`getent hosts github.com astral.sh files.pythonhosted.org`. Mitigation B —
offline wheelhouse (no network): see `docs/INSTALL.md`; build a Linux wheelhouse
on a connected box (`uv pip compile` + `uv pip download --python-platform
x86_64-unknown-linux-gnu`), transfer, then
`uv tool install --offline --find-links ./wheelhouse ...`.

## Dirty worktree errors

`LOCAL_WORKTREE_DIRTY` (executor) or `REMOTE_WORKTREE_DIRTY` (remote slot).
Confirm with `supervisor-cao task show <task-id>` and
`git -C ~/cao-worktrees/<project>/<task-id>/executor status`. The platform
refuses dirty worktrees and never runs `git reset --hard`/`git clean -fdx`;
commit or stash manually, then re-run. A remote slot reported
`DIRTY`/`UNHEALTHY` must be inspected, never recovered destructively.

## Lock timeouts

`REMOTE_ENV_LOCK_TIMEOUT` on a remote pool acquire (previous task crashed
holding the lock, or slot genuinely busy). Inspect on the remote host:

```bash
supervisor-cao task show <task-id>   # then on the remote host:
ssh <SSH_HOST> "ls -la <REMOTE_REPOSITORY_PATH>/.supervisor-cao-lock*"
```

If stale (no live task owns it), remove the lock file on the remote host and
re-run. Never use destructive cleanup; an `UNHEALTHY` slot must be
investigated, not force-recovered.

## Windows sync blocked

`WINDOWS_SYNC_BLOCKED`. Windows sync is fast-forward only and requires all 7
gates: (1) candidate pushed, (2) `tested_sha == candidate_sha`, (3)
`reviewed_sha == candidate_sha`, (4) Review `APPROVED`, (5) Draft PR created,
(6) Windows worktree clean, (7) local task branch fast-forwardable. Final
check: `Windows HEAD == candidate SHA`. `supervisor-cao task show <task-id>`
shows which gate failed. Common causes: a new executor commit invalidated
tested/reviewed SHA (re-run verify + review), Windows worktree dirty
(commit/stash manually), or branch divergence (investigate — never force). The
sync script never `reset --hard`, overwrites dirty, force-checks-out,
cherry-picks, or merges the base branch.

## Codex budget exhausted

`CODEX_BUDGET_EXHAUSTED`; task stops, requires human intervention. The budget
(4/task: planner 1 + full_review 1 + incremental_review 1 + judge 1) is
enforced in code; the Supervisor cannot self-track or bypass it. Check the call
log with `supervisor-cao task show <task-id>`. Recovery is human-only: raise
the budget (rare) or start a new task; never retry non-budget Codex failures
(lint, polling, formatting).

## CAO OpenCode provider experimental

Long-running worker messages delayed/missed; callback delivery unreliable. The
CAO OpenCode provider is experimental — multi-agent callback uses an inbox
polling fallback (CAO issues #203/#115); long-task delivery and recovery are
tested separately from the live path. The platform routes long polling through
deterministic runners (not LLMs). If a callback appears stuck, check the CAO
tmux session and artifacts under `~/cao-runs/<task-id>/`, then restart the
affected session (`supervisor-cao down` then `up`). State persists in SQLite
and resumes from the last legal state.

## gh auth / secret scan

**gh auth**: Draft PR creation fails; `gh` returns auth errors. Run
`gh auth status`, then `gh auth login` (needs write access on the task branch)
and retry from `DRAFT_PR_CREATED` onward.

**Secret scan blocking push**: `scripts/scan-secrets` exits 1. Run
`python3 scripts/scan-secrets` to see the offending file/pattern. It detects
API key formats (`sk-`, `AKIA`, `ghp_`, `AIza`, Bearer), private identifiers
(hosts, containers, usernames), path leaks, and forbidden files (`.env`,
`secrets.env`, `*.local.yaml`, `*.private.md`, `auth.json`). Move the data to
`~/.config/supervisor-cao/` and remove it from tracked files. The scanner
intentionally lists identifiers to detect them (skips itself and cache/build
dirs) — never edit it to silence a real finding.

---

# 故障排查

## cao-server 未启动

`supervisor-cao up` 打印 `FAILED`；`doctor` 显示 `cao-server down`。

```bash
pgrep -af cao-server; ss -ltnp 2>/dev/null | grep 9889   # running? port held?
cat /tmp/cao-server.log; tmux ls     # errors + leftover sessions
supervisor-cao down && pkill -f cao-server 2>/dev/null && supervisor-cao up
curl -s http://127.0.0.1:9889/health # expect 200
```

## OpenCode 提供商错误

Supervisor 无法连接 GLM/Qwen；`detect-models` 报告角色未配置。

```bash
ls -l ~/.local/share/opencode/auth.json    # auth present? (or $OPENCODE_AUTH_PATH)
opencode models                            # provider/model IDs detected?
python3 scripts/detect-models --check      # exit 2 if a role unconfigured
```

`detect-models` 永不打印 API key。某个角色显示 `configured: false` 表示
`scripts/detect-models` 中的 `candidates` 列表与 `opencode
models` 不匹配；扩展该列表或为缺失的提供商完成认证。CAO 将 OpenCode
配置隔离在 `~/.aws/opencode/`（与 `~/.config/opencode/` 分离）。

## Codex CLI 未找到

`doctor` 显示 `Codex CLI MISSING`；Codex planner/review 调用失败。将
`CODEX_BIN` 设置为绝对路径（WSL 侧或 `/mnt/c/...`）并重新运行 `doctor`：

```bash
export CODEX_BIN="/mnt/c/Users/<USER>/AppData/Local/bin/codex.exe"
supervisor-cao doctor    # Codex CLI should now be ok
```

## WSL2 网络 / DNS 问题

`uv tool install`/`git clone`/`pip download` 因 DNS 错误失败；某个 fake-ip
VPN 劫持了解析。缓解方案 A — DoH + `/etc/hosts`（在线但
DNS 损坏）：`sudo python3 .doh_hosts_final.py`，然后
`getent hosts github.com astral.sh files.pythonhosted.org`。缓解方案 B —
离线 wheelhouse（无网络）：参见 `docs/INSTALL.md`；在联网机器上构建 Linux wheelhouse
（`uv pip compile` + `uv pip download --python-platform
x86_64-unknown-linux-gnu`），传输，然后
`uv tool install --offline --find-links ./wheelhouse ...`。

## 脏 worktree 错误

`LOCAL_WORKTREE_DIRTY`（executor）或 `REMOTE_WORKTREE_DIRTY`（远程 slot）。
用 `supervisor-cao task show <task-id>` 和
`git -C ~/cao-worktrees/<project>/<task-id>/executor status` 确认。平台
拒绝脏 worktree 且永不运行 `git reset --hard`/`git clean -fdx`；
手动 commit 或 stash，然后重新运行。报告为
`DIRTY`/`UNHEALTHY` 的远程 slot 必须被检查，绝不能以破坏性方式恢复。

## 锁超时

在远程池获取时出现 `REMOTE_ENV_LOCK_TIMEOUT`（前一个任务崩溃
持有锁，或 slot 确实繁忙）。在远程主机上检查：

```bash
supervisor-cao task show <task-id>   # then on the remote host:
ssh <SSH_HOST> "ls -la <REMOTE_REPOSITORY_PATH>/.supervisor-cao-lock*"
```

如果为陈旧锁（无活跃任务持有），在远程主机上删除锁文件并
重新运行。绝不使用破坏性清理；`UNHEALTHY` slot 必须被
调查，而不是强制恢复。

## Windows 同步被阻止

`WINDOWS_SYNC_BLOCKED`。Windows 同步仅限 fast-forward 并要求全部 7 个
门禁：(1) candidate 已 push，(2) `tested_sha == candidate_sha`，(3)
`reviewed_sha == candidate_sha`，(4) 评审 `APPROVED`，(5) 已创建 Draft PR，
(6) Windows worktree 干净，(7) 本地任务分支可 fast-forward。最终
检查：`Windows HEAD == candidate SHA`。`supervisor-cao task show <task-id>`
显示哪个门禁失败。常见原因：新的 executor 提交使
tested/reviewed SHA 失效（重新运行验证 + 评审）、Windows worktree 脏
（手动 commit/stash）或分支分叉（调查 — 绝不强制）。同步脚本绝不
`reset --hard`、覆盖脏状态、强制 checkout、
cherry-pick 或合并基础分支。

## Codex 预算耗尽

`CODEX_BUDGET_EXHAUSTED`；任务停止，需要人工介入。预算
（4/任务：planner 1 + full_review 1 + incremental_review 1 + judge 1）在
代码中强制；Supervisor 无法自追踪或绕过它。用
`supervisor-cao task show <task-id>` 检查调用日志。恢复仅限人工：
提高预算（罕见）或开始新任务；绝不重试非预算 Codex 失败
（lint、轮询、格式化）。

## CAO OpenCode 提供商实验性

长时间运行的 worker 消息延迟/丢失；回调投递不可靠。
CAO OpenCode 提供商是实验性的 — 多智能体回调用 inbox
轮询回退（CAO issues #203/#115）；长任务投递和恢复与
实时路径分开测试。平台通过
确定性 runner（而非 LLM）路由长轮询。如果回调看起来卡住，检查 CAO
tmux 会话和 `~/cao-runs/<task-id>/` 下的工件，然后重启
受影响的会话（`supervisor-cao down` 然后 `up`）。状态持久化在 SQLite
中并从上一个合法状态恢复。

## gh auth / secret scan

**gh auth**：Draft PR 创建失败；`gh` 返回认证错误。运行
`gh auth status`，然后 `gh auth login`（需要对任务分支的写权限）
并从 `DRAFT_PR_CREATED` 起重试。

**Secret scan 阻止 push**：`scripts/scan-secrets` 退出码 1。运行
`python3 scripts/scan-secrets` 查看违规文件/模式。它检测
API key 格式（`sk-`、`AKIA`、`ghp_`、`AIza`、Bearer）、私有标识符
（主机、容器、用户名）、路径泄漏和禁止文件（`.env`、
`secrets.env`、`*.local.yaml`、`*.private.md`、`auth.json`）。将数据移至
`~/.config/supervisor-cao/` 并从被追踪文件中移除。扫描器有意
列出标识符以检测它们（跳过自身和缓存/构建
目录）— 绝不编辑它来压制真实 finding。
