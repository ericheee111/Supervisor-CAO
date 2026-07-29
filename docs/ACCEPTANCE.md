# Acceptance Criteria and Results

The platform is acceptable only after the evidence below exists. All tests are
run from the repo root on WSL2 Ubuntu-24.04.

## Test suite

```bash
python -m pytest tests/ -q
```

| Level | Count | Scope |
|-------|-------|-------|
| Unit | 51 | state machine, budget, schema, SHA, locks, windows-dirty, fast-forward, PR body, secret scan, config, permissions |
| Integration | 10 | planner, executor fix, verifier fail, stale, budget, pool, windows-blocked, happy path |
| E2E | 13 | temporary-repository full flow |
| Stability | 10/10 | short callback flow x10, one long-running worker, one timeout, one callback-recovery |

Current status: **all passing** (51 unit + 10 integration + 13 E2E + 10/10
stability).

## Unit tests (51)

Cover every deterministic enforcement path:

- **State machine**: legal forward transitions only; no skipping states;
  `tested_sha == candidate_sha`; `reviewed_sha == tested_sha`; new candidate
  invalidates tested/reviewed; gate checks before `LOCAL_VERIFIED`,
  `REMOTE_VERIFIED`, `APPROVED`, `DRAFT_PR_CREATED`; error states reachable
  from any non-terminal state; full audit log.
- **Budget**: per-task, per-role Codex cap; atomic spend under lock;
  `CODEX_BUDGET_EXHAUSTED` raised on overflow; persisted call log.
- **Schema**: `task.schema.json`, `plan.schema.json`, `implementation.schema.json`,
  `verification.schema.json`, `review.schema.json`, `decision.schema.json`.
- **SHA**: candidate/tested/reviewed binding; invalidation on new commit.
- **Locks**: remote pool atomic lock; stale lock handling.
- **Windows-dirty**: refuse to operate on dirty worktrees.
- **Fast-forward**: ff-only sync; reject non-fast-forward.
- **PR body**: draft PR gating.
- **Secret scan**: detect API keys, private identifiers, forbidden files.
- **Config**: layered example + local + task override loading.
- **Permissions**: role-based source/git/remote access.

## Integration tests (10)

- `planner` — Codex planner callback and plan artifact.
- `executor-fix` — executor applies a fix and re-verifies.
- `verifier-fail` — verifier reports failure routes back to fixing.
- `stale` — stale lock / stale SHA handling.
- `budget` — budget exhaustion stops the task.
- `pool` — remote pool lock acquire/release and restoration.
- `windows-blocked` — Windows sync refuses when gates fail.
- `happy-path` — full non-terminal flow on fixtures.
- (plus two more covering report compression and dispute arbitration).

## E2E tests (13)

Temporary-repository full flow demonstrates:

```
Supervisor -> Codex Planner -> GLM Executor -> Qwen Verifier
-> Codex Reviewer -> controlled fix cycle -> re-verification
-> incremental review -> Draft PR path -> protected sync path
```

No live destructive tests against the real project repository. The real-project
integration test is read-only unless a human explicitly starts a real task.

## Stability (10/10)

- Short callback flow repeated 10x (no drift, no leaked state).
- One long-running worker scenario.
- One timeout scenario.
- One callback-recovery scenario.
- Known CAO/OpenCode limitations documented honestly (see below).

## Pandas read-only smoke

Confirms, without modifying anything:

- Project configuration loads (example + local layering).
- Configured base branch (`dev`) is reachable.
- Local repositories can be inspected safely.
- Remote validation slots can be health-checked.

Result: **3 PASS** (config load, base branch reachable, local repo inspectable)
+ **1 LIMITATION** (remote pool/containers/conda over SSH — see known
limitations).

## Supervisor benchmark

`scripts/supervisor-benchmark` exercises the Supervisor role with both cheap
providers against canned tasks.

- **GLM (primary Supervisor)**: 4/4 tasks completed correctly.
- **Qwen (backup Supervisor)**: 4/4 tasks completed correctly.
- **Qwen as primary** (per model map): 4/4.

Both providers are viable Supervisors. GLM is the configured primary; Qwen is
the configured backup.

## Known limitations

These do not block acceptance of the core workflow; they are documented
honestly per the stability criteria.

1. **Remote SSH not configured.** The remote validation pool (containers, conda
   environment) over SSH is a `LIMITATION`. Remote-pool acquire/release and
   restoration are covered by unit/integration tests with fixtures; the live
   remote path is not exercised end-to-end because SSH to the validation host
   is not configured in this environment. Status: `READY_WITH_KNOWN_LIMITATIONS`
   for remote-pool-dependent tasks.
2. **WSL2 network restricted.** A fake-ip VPN hijacks DNS in this environment.
   The offline wheelhouse install path (see `docs/INSTALL.md`) and DoH +
   `/etc/hosts` mitigation (see `docs/TROUBLESHOOTING.md`) are used. CAO and
   providers were installed offline.
3. **Codex CLI on Windows path.** `codex` is not on the WSL PATH by default.
   Set `CODEX_BIN` to the absolute path (WSL or `/mnt/c/...`). `supervisor-cao
   doctor` honors `CODEX_BIN`.
4. **CAO OpenCode provider is experimental.** Multi-agent callback uses inbox
   polling fallback (CAO issues #203/#115); long-task message delivery and
   callback recovery are tested separately, not via the live callback path.

## Security acceptance

- Secret scanner (`scripts/scan-secrets`) passes on all tracked files.
- Private deployment files (`*.local.yaml`, `models.local.yaml`, `secrets.env`,
  `*.private.md`, `auth.json`) are git-ignored.
- No private infrastructure identifiers (internal hosts, container names,
  usernames, private paths) appear in tracked files.

## Final status

- `READY` — all mandatory checks pass.
- `READY_WITH_KNOWN_LIMITATIONS` — core workflow works; documented
  non-critical limitation remains (remote SSH pool).
- `BLOCKED` — a mandatory capability cannot be completed.

Current overall status: **READY_WITH_KNOWN_LIMITATIONS** (remote SSH pool not
configured live; all local, unit, integration, E2E, and stability tests pass).

## See also

- `docs/USER_GUIDE.md` — workflow and task files.
- `docs/TROUBLESHOOTING.md` — diagnosing failures.
- `docs/SECURITY.md` — role permissions and forbidden operations.
