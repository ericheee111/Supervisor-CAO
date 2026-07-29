# Adding a Project

A project integration should add configuration and plugins without changing core orchestration behavior.

## Required configuration

- Project name.
- Repository URL.
- Default base branch.
- WSL clone location.
- Worktree root.
- Task-branch naming policy.
- Optional protected local repository.
- Local quick-verification commands.
- Remote validation-pool definition.
- Required task fields such as baseline, selectors, or acceptance thresholds.

## Required implementation points

1. Add a sanitized example config under `config/examples/`.
2. Keep real deployment values in a local ignored config.
3. Add validation adapters only when generic command execution is insufficient.
4. Add unit and E2E fixtures.
5. Document project-specific requirements.
6. Confirm that the real-project setup smoke test is read-only.

Core state, budgets, review gates, and synchronization safety must remain project-independent.
