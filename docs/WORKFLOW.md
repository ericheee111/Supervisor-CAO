# Workflow

## Normal path

```text
CREATED
→ RESEARCHING
→ PLANNING
→ PLAN_READY
→ IMPLEMENTING
→ IMPLEMENTED
→ LOCAL_VERIFYING
→ LOCAL_VERIFIED
→ REMOTE_QUEUED
→ REMOTE_VERIFYING
→ REMOTE_VERIFIED
→ REVIEWING
→ APPROVED
→ DRAFT_PR_CREATED
→ WINDOWS_SYNCED
→ READY_FOR_HUMAN_REVIEW
```

## Repair path

```text
REVIEWING
→ CHANGES_REQUESTED
→ FIXING
→ LOCAL_VERIFYING
→ REMOTE_VERIFYING
→ INCREMENTAL_REVIEWING
→ APPROVED
```

## Human-intervention conditions

- Missing performance baseline or acceptance threshold.
- Dirty local or remote repository.
- Codex budget exhausted.
- No progress after configured rounds.
- Remote pool unavailable.
- High-severity dispute not resolved within the bounded protocol.
- Protected local-repository synchronization blocked.

## Dispute protocol

```text
Reviewer finding
→ Executor response with evidence
→ Reviewer rebuttal with new evidence
→ Judge decision
```

No free-form or unlimited agent debate.
