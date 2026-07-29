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
~/cao-runs/                        # logs, test results, ASV, audit records
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
- LLMs never poll long-running ASV; deterministic runners execute and collect

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
