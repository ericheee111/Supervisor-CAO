# AGENTS.md

## Mission

Build and maintain `Supervisor-CAO`, a generic, safety-first multi-agent software-development platform based on CAO.

This repository is public. Keep platform code generic and keep machine-specific deployment data outside Git.

The private implementation requirements are stored locally in:

```text
Supervisor-CAO项目设计方案.private.md
```

Read that file before making architectural or installation changes. It must never be committed.

---

## Non-negotiable rules

1. **Do not stop at planning or scaffolding.** Implement, test, document, commit, and push meaningful milestones.
2. **Do not expose secrets or internal infrastructure.** Never commit API keys, tokens, cookies, authentication files, internal host aliases, container names, usernames, private paths, or private logs.
3. **Do not damage existing repositories.**
   - No `git reset --hard`.
   - No `git clean -fdx`.
   - No force push.
   - No automatic merge.
   - Do not overwrite dirty working trees.
4. **Do not trust natural-language claims.** Success requires exit codes, artifacts, exact Git SHAs, and valid state transitions.
5. **Do not use an LLM for deterministic policy.** Budgets, locks, SHA matching, state transitions, sync safety, and permissions must be enforced in code.
6. **Do not add ZCode to the runtime architecture.** ZCode is only the bootstrap and implementation environment.
7. **Do not make Codex the persistent supervisor.** Codex is limited to Planner, Reviewer, Incremental Reviewer, and exceptional Judge roles.
8. **Do not auto-merge PRs or update the base branch.** Stop at `READY_FOR_HUMAN_REVIEW`.

---

## Required execution order

For substantial changes:

1. Inspect the repository, current configuration, and relevant upstream CAO behavior.
2. Write or update an implementation checklist in the task notes.
3. Make the smallest coherent change.
4. Run focused tests.
5. Run broader affected tests.
6. Run formatting, linting, type checking, and secret scanning.
7. Review the diff for unrelated changes.
8. Commit and push a meaningful checkpoint.
9. Report the commit SHA and remaining work.

Do not combine unrelated refactors with the current task.

---

## Repository boundaries

### Public and versioned

Safe to commit:

- Generic source code.
- Agent profiles without credentials or internal paths.
- JSON/YAML schemas.
- Sanitized example project configuration.
- Tests and fake fixtures.
- Public documentation.
- CAO pinned commit metadata.
- Upgrade and rollback logic.

### Local and ignored

Must remain outside Git:

```text
~/.config/supervisor-cao/
~/.local/state/supervisor-cao/
~/cao-runs/
*.local.yaml
*.private.md
secrets.env
.env
```

Before every push, run the repository secret scanner and search explicitly for known private identifiers.

---

## Architecture constraints

The intended flow is:

```text
GLM/Qwen Supervisor
→ cheap research
→ Codex Planner
→ GLM Executor
→ Qwen Verifier
→ Codex Reviewer
→ optional GLM fix
→ Qwen re-verification
→ optional Codex incremental review
→ Draft PR
→ protected Windows branch sync
→ READY_FOR_HUMAN_REVIEW
```

The runtime must include a deterministic policy layer that owns:

- Task state.
- Legal transitions.
- Codex budgets.
- Worktree lifecycle.
- Candidate/tested/reviewed SHA matching.
- Remote pool locks.
- Retry and no-progress limits.
- Draft PR gates.
- Windows synchronization gates.
- Audit events.

Supervisor prompts may explain these rules, but prompts are not enforcement.

---

## Role permissions

### Supervisor

- May use approved orchestration tools.
- May read task state and structured artifacts.
- Must not edit source code.
- Must not directly run arbitrary Git, SSH, Docker, or Windows synchronization commands.
- Must not bypass policy gates.

### Researcher / Planner / Reviewer / Judge

- Read-only source access.
- Read-only Git commands.
- No editing, commits, pushes, merges, or destructive remote commands.

### Executor

- May edit only its assigned worktree.
- May build, test, commit, and push the task branch.
- No force push.
- No base-branch changes.
- No Windows repository operations.
- No `sudo`.
- No benchmark manipulation to manufacture success.

### Verifier

- Read-only source access.
- May invoke approved verification scripts.
- No source edits, commits, or pushes.
- No remote cleanup.

---

## Git rules

- Base branch is project configuration, not hard-coded globally.
- The default base branch is `main` unless a project config overrides it.
- Task branches use `agent/<task-id>`.
- Every valid candidate must be committed and pushed.
- Verification and review results are valid only for their exact SHA.
- Any new commit invalidates older verification and review.
- Never share one worktree between writable and read-only roles.
- Refuse to operate on dirty worktrees unless the operation is explicitly read-only.

---

## Testing requirements

Every feature needs tests at the appropriate levels:

- Unit tests for state, budgets, configuration, locks, and safety gates.
- Integration tests for worker results, callback handling, and failure recovery.
- End-to-end tests using temporary repositories.
- No live destructive tests against real project repositories.
- Real project integration tests are read-only unless the user explicitly starts a real task.

Test output must be captured as artifacts. Do not report success from memory or from an agent summary alone.

---

## Remote environment rules

Remote validation environments are shared resources.

- Acquire an atomic lock before changing repository state.
- Record original branch, HEAD, and cleanliness.
- Refuse dirty repositories.
- Restore original branch and HEAD.
- Validate restoration before releasing the lock.
- Mark the slot unhealthy if restoration fails.
- Never use destructive cleanup to recover.
- Keep LLMs out of long-running polling loops; use deterministic runners.

---

## Codex budget

Default maximum per task:

```yaml
planner: 1
full_review: 1
incremental_review: 1
judge: 1
total: 4
```

The budget is enforced by code and persisted.

Do not spend Codex calls on:

- Polling.
- Log formatting.
- Fixed-threshold calculations.
- Lint failures.
- Ordinary retries.
- Status routing.
- Message forwarding.

---

## Review disputes

No free-form group chat.

Maximum dispute sequence:

```text
Reviewer finding
→ Executor response with evidence
→ Reviewer rebuttal with new evidence
→ Judge decision
```

A finding needs a stable ID, severity, file/line, failure scenario, evidence, and recommended direction.

No new evidence means no additional round.

---

## Documentation requirements

Update documentation with behavior changes.

At minimum maintain:

- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/INSTALL.md`
- `docs/USER_GUIDE.md`
- `docs/ADD_PROJECT.md`
- `docs/ACCEPTANCE.md`
- `docs/SECURITY.md`
- `docs/TROUBLESHOOTING.md`

Commands in docs must be tested or clearly marked as examples.

---

## Progress updates

After each major milestone, report:

- What was implemented.
- Tests actually run and their result.
- Commit SHA.
- Current blockers or risks.
- Next milestone.

Only use final status values:

```text
READY
READY_WITH_KNOWN_LIMITATIONS
BLOCKED
```
