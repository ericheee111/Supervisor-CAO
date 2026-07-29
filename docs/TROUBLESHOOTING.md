# Troubleshooting

Symptom -> cause -> fix. All commands run in WSL2 Ubuntu-24.04.

## cao-server not starting

Symptom: `supervisor-cao up` prints `FAILED to start cao-server`; `doctor`
shows `cao-server down`.

```bash
pgrep -af cao-server                 # is it already running?
ss -ltnp 2>/dev/null | grep 9889     # is the port held?
cat /tmp/cao-server.log              # last startup errors
tmux ls                              # leftover CAO tmux sessions?
```

Fix:

```bash
supervisor-cao down                  # clears CAO tmux sessions
pkill -f cao-server 2>/dev/null      # kill a stale server
supervisor-cao up
curl -s http://127.0.0.1:9889/health # expect 200
```

If the port is held by another process, free it or set CAO's bind port per
upstream CAO docs before restarting.

## OpenCode provider errors

Symptom: Supervisor cannot reach GLM/Qwen; `detect-models` reports unconfigured
roles; CAO logs show provider auth failures.

```bash
# 1. Auth file present and readable?
ls -l ~/.local/share/opencode/auth.json
# (or the path in $OPENCODE_AUTH_PATH if OpenCode is shared from Windows)

# 2. Provider/model IDs detected?
opencode models

# 3. Regenerate the desensitized model map
python3 scripts/detect-models --check
python3 scripts/detect-models   # writes ~/.config/supervisor-cao/models.local.yaml
```

`detect-models` never prints API keys — it reads only provider/model IDs. If a
role shows `configured: false`, the candidate model list in
`scripts/detect-models` does not match what `opencode models` reports; extend
the `candidates` list or authenticate the missing provider. CAO isolates its
OpenCode config at `~/.aws/opencode/`, separate from `~/.config/opencode/`.

## Codex CLI not found

Symptom: `doctor` shows `Codex CLI MISSING`; Codex planner/review calls fail.

```bash
codex --version          # on WSL PATH?
echo "$CODEX_BIN"        # absolute path override
```

Fix: set `CODEX_BIN` to the absolute path (WSL-side or `/mnt/c/...` for a
Windows install):

```bash
export CODEX_BIN="/mnt/c/Users/<USER>/AppData/Local/bin/codex.exe"
# persist in ~/.bashrc, then:
supervisor-cao doctor    # Codex CLI should now be ok
```

## WSL2 network / DNS issues

Symptom: `uv tool install`, `git clone`, or `pip download` fail with DNS
errors; a fake-ip VPN hijacks resolution.

Mitigation A — DoH + `/etc/hosts` (online but DNS-broken):

```bash
# Resolve critical hosts over DoH and pin them in /etc/hosts
sudo python3 .doh_hosts_final.py   # or your DoH resolver
# Verify:
getent hosts github.com astral.sh files.pythonhosted.org
```

Mitigation B — offline wheelhouse (no network at all). See
`docs/INSTALL.md` (Offline / restricted-network install): build a Linux
wheelhouse on a connected box with `uv pip compile` + `uv pip download
--python-platform x86_64-unknown-linux-gnu`, transfer it, then
`uv tool install --offline --find-links ./wheelhouse ...`.

## Dirty worktree errors

Symptom: `LOCAL_WORKTREE_DIRTY` (executor worktree) or
`REMOTE_WORKTREE_DIRTY` (remote pool slot).

```bash
supervisor-cao task show <task-id>   # confirm the error state
# Inspect the affected worktree
git -C ~/cao-worktrees/<project>/<task-id>/executor status
```

The platform refuses to operate on dirty worktrees and never runs
`git reset --hard` or `git clean -fdx`. Fix by committing or stashing the
uncommitted change manually, then re-run the task. For a remote slot reported
`DIRTY` or `UNHEALTHY`, the pool marks it and refuses use; restoration failure
is never recovered with destructive cleanup — investigate the slot manually.

## Lock timeouts

Symptom: `REMOTE_ENV_LOCK_TIMEOUT` on a remote pool acquire.

Cause: a previous task crashed while holding the remote lock, or a slot is
genuinely busy.

```bash
supervisor-cao task show <task-id>   # event log shows the acquire attempt
# Inspect the remote lock state (on the validation host, manually):
ssh <SSH_HOST> "ls -la <REMOTE_REPOSITORY_PATH>/.supervisor-cao-lock*"
```

If the lock is stale (no live task owns it), remove the stale lock file
manually on the remote host and re-run. Do not use destructive cleanup as a
recovery shortcut. If restoration previously failed, the slot is `UNHEALTHY`
and must be inspected, not force-recovered.

## Windows sync blocked

Symptom: `WINDOWS_SYNC_BLOCKED`. Windows sync is fast-forward only and
requires all 7 gates to pass:

1. candidate pushed to remote
2. `tested_sha == candidate_sha`
3. `reviewed_sha == candidate_sha`
4. Review `APPROVED`
5. Draft PR created
6. Windows worktree clean
7. Local task branch fast-forwardable

Final check: `Windows HEAD == candidate SHA`.

```bash
supervisor-cao task show <task-id>   # which gate failed?
```

Common causes: a new executor commit invalidated `tested_sha`/`reviewed_sha`
(re-run verify + review), the Windows worktree has uncommitted changes (commit
or stash them manually), or the Windows branch is not fast-forwardable (do not
force anything — investigate why it diverged). The sync script never
`reset --hard`, never overwrites dirty, never force-checks-out, never
cherry-picks, never merges the base branch.

## Codex budget exhausted

Symptom: `CODEX_BUDGET_EXHAUSTED`; task stops and requires human intervention.

The budget (4 calls/task: planner 1 + full_review 1 + incremental_review 1 +
judge 1) is enforced in code; the Supervisor cannot self-track or bypass it.

```bash
supervisor-cao task show <task-id>   # codex call log + remaining budget
```

Recovery is human-only: either raise the budget in project config (rare,
justified) or close the task and start a new one. Do not retry Codex calls
that failed for non-budget reasons (lint, polling, formatting) — those should
never have spent a Codex call.

## CAO OpenCode provider experimental

Symptom: long-running worker messages are delayed or appear to be missed;
callback delivery is unreliable.

Cause: the CAO OpenCode provider is experimental; multi-agent callback uses an
inbox polling fallback (CAO issues #203/#115). Long-task message delivery and
callback recovery are tested separately from the live callback path.

Mitigation: the platform already routes long-running polling through
deterministic runners (not LLMs). If a callback appears stuck, check the CAO
tmux session and the run artifacts under `~/cao-runs/<task-id>/`. Restarting
the affected CAO session (`supervisor-cao down` then `up`) re-initializes the
inbox; task state persists in SQLite and resumes from the last legal state.

## gh auth issues

Symptom: Draft PR creation fails; `gh` commands return auth errors.

```bash
gh auth status          # authenticated?
gh auth login           # re-authenticate if needed
```

Draft PR creation requires `gh` authenticated with repo write access on the
task branch. Re-run `gh auth login` (browser or token flow) and retry the task
from `DRAFT_PR_CREATED` onward.

## Secret scan blocking push

Symptom: `scripts/scan-secrets` exits 1 and blocks a push.

```bash
python3 scripts/scan-secrets   # shows the offending file/pattern
```

The scanner detects API key formats (`sk-`, `AKIA`, `ghp_`, `AIza`, Bearer),
known private identifiers (internal hosts, container names, usernames),
private path leaks, and forbidden files (`.env`, `secrets.env`, `*.local.yaml`,
`*.private.md`, `auth.json`). Fix the leak by moving the data to
`~/.config/supervisor-cao/` and removing it from tracked files. The scanner
intentionally lists identifiers to detect them, so it skips itself and common
cache/build directories — do not edit the scanner to silence a real finding.

## See also

- `docs/INSTALL.md` — setup and offline install.
- `docs/USER_GUIDE.md` — workflow and error states.
- `docs/SECURITY.md` — what may never be committed.
