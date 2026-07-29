# Adding a Project

A project integration adds **configuration and (rarely) validation adapters** —
it must not change core orchestration behavior. The platform stays generic:
state machine, budgets, SHA checks, locks, review gates, and sync safety are
project-independent.

## Principles

1. **Public repo stays sanitized.** No real hosts, container names, usernames,
   or private paths in tracked files.
2. **Private data lives in `~/.config/supervisor-cao/`.** Layered local config
   fills in real values at runtime.
3. **No project-specific code in the policy layer.** If generic command
   execution is insufficient, add a thin validation adapter — never a fork of
   the state machine or budget logic.

## Step 1 — Create the sanitized example config

Add `config/examples/<project>.example.yaml`. Use placeholders for everything
machine-specific. This file is committed.

Example (pandas), abbreviated from `config/examples/pandas.example.yaml`:

```yaml
name: pandas
base_branch: dev
task_branch_prefix: agent/
wsl_repo: "~/projects/pandas"                 # placeholder; real value is private
windows_repo: "<WINDOWS_REPO_PATH>"           # placeholder

default_verification:
  wsl_quick:      { build: true, focused_pytest: true, candidate_sha_check: true }
  remote_pool:    { editable_install: true, correctness_tests: true, asv: true, log_collection: true }
  report:         { structured: true }

remote_validation:
  ssh_host: "<SSH_HOST>"
  containers: ["<CONTAINER_A>", "<CONTAINER_B>"]
  user: "<REMOTE_USER>"
  repo_path: "<REMOTE_REPOSITORY_PATH>"
  conda_env: "<CONDA_ENVIRONMENT>"

executor_limits:
  max_rounds: 8
  max_no_progress_rounds: 2
  push_every_valid_candidate: true
  require_clean_worktree: true
  require_commit: true

codex_budget:
  max_calls_per_task: 4
  planner: 1
  full_review: 1
  incremental_review: 1
  judge: 1
```

## Step 2 — Create the private local config

Copy the example and fill in real values. This file is git-ignored.

```bash
mkdir -p ~/.config/supervisor-cao/projects
cp config/examples/pandas.example.yaml \
   ~/.config/supervisor-cao/projects/pandas.local.yaml
```

Edit the local file to set real values:

```yaml
name: pandas
base_branch: dev
wsl_repo: "/home/<USER>/projects/pandas"
windows_repo: "/mnt/c/projects/pandas"
remote_validation:
  ssh_host: "<SSH_HOST>"                       # real alias from ~/.ssh/config
  containers: ["<REAL_CONTAINER_A>", "<REAL_CONTAINER_B>"]
  user: "<REMOTE_USER>"
  repo_path: "/home/<REMOTE_USER>/pandas"
  conda_env: "<CONDA_ENVIRONMENT>"
```

## Step 3 — (Optional) Add validation rules/plugins

Only if generic command execution is insufficient, add a thin adapter under
`src/supervisor_cao/validation/`. Prefer declaring commands and selectors in
the YAML over writing project-specific Python. The policy layer (state,
budget, SHA, locks, sync gates) must remain untouched.

## Step 4 — Add tests and fixtures

- Unit tests for any new config schema fields.
- E2E fixture using a temporary repository (never the real project repo).
- Confirm the real-project setup smoke test is **read-only**: it loads config,
  checks the base branch is reachable, inspects local repos, and health-checks
  remote slots without modifying anything.

## Config schema reference

| Field | Purpose |
|-------|---------|
| `name` | Project identifier (matches `supervisor-cao chat <name>`). |
| `base_branch` | Base branch task branches are cut from (e.g. `dev`). Never rewritten. |
| `task_branch_prefix` | Prefix for task branches (default `agent/`). |
| `wsl_repo` | WSL Linux filesystem path to the agent's clone. |
| `windows_repo` | Windows repo path the sync script fast-forwards (read from local config). |
| `remote_validation` | SSH host, containers, user, repo path, conda env for the remote pool. |
| `default_verification` | WSL quick checks, remote pool checks, reporting toggles. |
| `executor_limits` | `max_rounds`, `max_no_progress_rounds`, push/clean/commit requirements. |
| `codex_budget` | Per-task Codex call caps per role (enforced in code). |

## Layering

Config is loaded in order, later wins:

1. `config/examples/<project>.example.yaml` (public, sanitized)
2. `~/.config/supervisor-cao/projects/<project>.local.yaml` (private, real values)
3. Task-level overrides (from the task file)

## Task-level overrides

Task files may tighten verification but must supply the performance quartet
(see `docs/USER_GUIDE.md`): `baseline_sha`, `benchmark_selector`,
`performance_acceptance`, `required_test_scope` (plus `regression_threshold`).
Missing critical performance parameters route the task to `NEEDS_HUMAN`.

## Checklist before declaring a project integrated

- [ ] Sanitized example committed under `config/examples/`.
- [ ] Private local config created and git-ignored.
- [ ] `supervisor-cao doctor` lists the project.
- [ ] Read-only smoke test passes (config loads, base branch reachable, remote
      slots health-checked, nothing modified).
- [ ] Secret scanner (`scripts/scan-secrets`) passes on the example file.
- [ ] No project-specific code added to the policy layer.

## See also

- `docs/USER_GUIDE.md` — running tasks.
- `docs/SECURITY.md` — what may never be committed.
- `docs/ACCEPTANCE.md` — acceptance criteria.
