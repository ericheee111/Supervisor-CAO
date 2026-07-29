# Acceptance Criteria

The platform is acceptable only after the following evidence exists.

## Environment

- WSL2 runtime prerequisites are detected and reported.
- CAO is installed from upstream and pinned to a tested commit.
- OpenCode and Codex compatibility smoke tests pass.
- Provider/model mappings are detected without exposing credentials.

## Policy layer

- Legal task-state transitions are enforced.
- Codex calls are counted and capped.
- Candidate, tested, and reviewed SHAs are validated.
- Retry and no-progress limits work.
- Dangerous Git and synchronization operations are rejected.

## Agent workflow

A temporary-repository E2E test demonstrates:

```text
Supervisor
→ Codex Planner
→ GLM Executor
→ Qwen Verifier
→ Codex Reviewer
→ controlled fix cycle
→ re-verification
→ incremental review
→ Draft PR path
→ protected synchronization path
```

## Stability

- Short callback flow is repeated at least ten times.
- One long-running worker scenario is tested.
- One timeout and one callback-recovery scenario are tested.
- Known CAO/OpenCode limitations are documented honestly.

## Project integration

The pandas smoke test is read-only and confirms:

- Project configuration loads.
- The configured base branch is reachable.
- Local repositories can be inspected safely.
- Remote validation slots can be health-checked.
- No real pandas code, branch, PR, or full benchmark run is modified during setup acceptance.

## Security

- Secret scanning passes.
- Private deployment files are ignored.
- No private infrastructure identifiers appear in tracked files.

## Final status

- `READY`: all mandatory checks pass.
- `READY_WITH_KNOWN_LIMITATIONS`: core workflow works, but documented non-critical instability remains.
- `BLOCKED`: a mandatory capability cannot be completed.
