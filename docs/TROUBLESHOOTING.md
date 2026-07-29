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
