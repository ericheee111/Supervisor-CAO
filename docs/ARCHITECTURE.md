# Architecture

`Supervisor-CAO` is a generic orchestration and policy layer for multi-agent software-development workflows.

## Goals

- Use CAO to run isolated CLI-agent sessions.
- Use lower-cost GLM/Qwen workers for orchestration, implementation, testing, and report compression.
- Reserve Codex for planning, final review, incremental review, and exceptional dispute resolution.
- Enforce state, budgets, Git SHA consistency, permissions, locks, and synchronization in deterministic code.
- Support projects through configuration and validation plugins rather than project-specific core logic.

## High-level flow

```text
User
  ↓
GLM/Qwen Supervisor
  ↓
Policy and state layer
  ├── Researcher
  ├── Codex Planner
  ├── GLM Executor
  ├── Qwen Verifier
  ├── Codex Reviewer
  └── Codex Judge
```

Typical lifecycle:

```text
Research
→ Plan
→ Implement
→ Local Verify
→ Remote Verify
→ Review
→ Fix/Reverify when needed
→ Draft PR
→ Protected local-repository synchronization
→ Human review
```

## Core invariants

- A verification result is valid only for its exact candidate SHA.
- A review is valid only for the exact verified SHA.
- A new commit invalidates older verification and review artifacts.
- No role may bypass the deterministic state machine.
- No LLM owns the Codex budget, remote lock, or synchronization gate.
- No automatic merge is performed.

## Public versus local configuration

The public repository contains generic schemas and sanitized examples.

Real provider IDs, credentials, hostnames, containers, usernames, paths, and run logs are stored outside Git under the user's local configuration and state directories.
