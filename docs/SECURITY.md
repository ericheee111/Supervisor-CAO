# Security

## Trust boundaries

The platform coordinates external model providers, local CLI tools, Git repositories, remote validation hosts, and Windows/WSL filesystems. Every boundary is treated as potentially unsafe.

## Secrets

Never commit:

- API keys or tokens.
- Provider configuration containing credentials.
- Codex/OpenCode authentication state.
- Private deployment specifications.
- Internal hosts, containers, usernames, or paths.
- Raw logs that may contain credentials.

## Git safety

Forbidden operations in automated workflows:

```text
git reset --hard
git clean -fdx
git push --force
automatic merge
overwriting dirty worktrees
```

Task branches may be pushed; base branches may not be rewritten.

## Role isolation

Writable source access is limited to the Executor worktree.

Planner, Reviewer, Judge, and Verifier roles are read-only. Remote access is exposed through approved scripts rather than arbitrary shell execution.

## Remote validation

A remote slot requires an atomic lock, cleanliness check, original-state snapshot, and verified restoration. Restoration failure marks the slot unhealthy.

## Public repository hygiene

Run secret scanning before every push. Public examples use placeholders only. Real values belong under the local configuration directory outside Git.
