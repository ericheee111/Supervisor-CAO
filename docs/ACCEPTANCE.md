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
