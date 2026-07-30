"""supervisor-cao-policy: the deterministic policy-layer stdio MCP server.

Requirement 1: this server is named ``supervisor-cao-policy`` — NOT
``cao-mcp-server``. The built-in ``@cao-mcp-server`` (which provides
``assign``/``handoff``/``send_message``) is deliberately NOT enabled on the
Supervisor profile, so the Supervisor cannot bypass the policy layer. The
Supervisor's only orchestration surface is this server's tools:

    create_task(task_id, project, description, baseline_sha?)
    run_next_stage(task_id)      — drives exactly one stage via a real CAO Worker
    get_task(task_id)
    get_artifact(task_id, name)
    resume_task(task_id)         — idempotent re-entry (no re-run of COMPLETED stages)

The server delegates to ``PolicyGateway``, which enforces the state machine,
Codex budget, SHA matching, worktree isolation, schema validation, and sync
gates in code. The Supervisor has no arbitrary bash and cannot bypass gates.

Run standalone for debugging:
    supervisor-cao-policy-mcp

CAO registers it via the Supervisor profile frontmatter:
    mcpServers:
      supervisor-cao-policy:
        type: stdio
        command: supervisor-cao-policy-mcp
        args: []
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

# Add src to path for direct execution (console script may not set PYTHONPATH)
_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fastmcp import FastMCP  # type: ignore[import-untyped]
from pydantic import Field  # type: ignore[import-untyped]

from supervisor_cao.mcp.policy_gateway import PolicyGateway, PolicyError
from supervisor_cao.state.machine import IllegalTransition, ShaMismatch

logger = logging.getLogger("supervisor-cao-policy")

# The shared gateway instance. Tools are thin wrappers that delegate to it,
# catching policy errors and returning structured results.
_gateway = PolicyGateway()

mcp = FastMCP(
    "supervisor-cao-policy",
    instructions="""
    # Supervisor-CAO Policy MCP Server

    This server is the ONLY orchestration surface the Supervisor may use. It
    enforces the deterministic policy layer: state machine, Codex budget, SHA
    matching, worktree isolation, schema validation, and sync gates.

    ## Tools

    - create_task: create a new task (initial state CREATED)
    - run_next_stage: drive exactly one pipeline stage forward via a real CAO
      Worker. Returns the updated task record. Call repeatedly until the state
      is READY_FOR_HUMAN_REVIEW or an error state.
    - get_task: read the current task record (state, SHAs, error)
    - get_artifact: read a stage artifact (research/plan/implementation/
      verification/review/codex-budget-summary) by name
    - resume_task: idempotently re-enter run_next_stage after a crash or
      timeout. COMPLETED stages are never re-run; Codex budget is never
      re-spent; no duplicate commits/PRs/Windows-syncs.

    ## Rules

    - You cannot edit source code, run git, ssh, or arbitrary shell commands.
    - You cannot bypass budget, review, or human-review gates.
    - A stage is done only when the policy layer's state says it is done AND
      the expected artifact exists.
    - Stop at READY_FOR_HUMAN_REVIEW. Summarize artifacts and state, then yield.
    """,
)


@mcp.tool()
def create_task(
    task_id: str = Field(description="Unique task identifier"),
    project: str = Field(description="Project name (must be configured)"),
    description: str = Field(description="Natural-language task description"),
    baseline_sha: str | None = Field(default=None, description="Baseline git SHA"),
) -> dict[str, Any]:
    """Create a new task. Returns the initial task record (state=CREATED)."""
    try:
        return _gateway.create_task(task_id, project, description, baseline_sha)
    except PolicyError as e:
        return {"error": str(e), "error_state": "PR_CREATION_FAILED"}


@mcp.tool()
def run_next_stage(
    task_id: str = Field(description="Task to advance by one stage"),
) -> dict[str, Any]:
    """Drive exactly one pipeline stage forward via a real CAO Worker.

    Reads the current state, dispatches to the correct Worker (researcher /
    codex-planner / glm-executor / qwen-verifier / codex-reviewer), validates
    the artifact against its JSON schema, and advances the state machine with
    real SHAs. Returns the updated task record. Call repeatedly until
    READY_FOR_HUMAN_REVIEW or an error state.
    """
    try:
        return _gateway.run_next_stage(task_id)
    except PolicyError as e:
        return {"error": str(e), "task_id": task_id}
    except (IllegalTransition, ShaMismatch) as e:
        return {"error": f"state error: {e}", "task_id": task_id}


@mcp.tool()
def get_task(
    task_id: str = Field(description="Task to inspect"),
) -> dict[str, Any]:
    """Read the current task record: state, baseline/candidate/tested/reviewed
    SHAs, error, and timestamps. Returns null if the task does not exist."""
    rec = _gateway.get_task(task_id)
    return rec if rec is not None else {"error": f"task not found: {task_id}"}


@mcp.tool()
def get_artifact(
    task_id: str = Field(description="Task whose artifact to read"),
    name: str = Field(description="Artifact name without .json (e.g. plan, review)"),
) -> dict[str, Any]:
    """Read a stage artifact JSON from the task's run dir. Returns null if the
    artifact does not exist."""
    art = _gateway.get_artifact(task_id, name)
    return art if art is not None else {"error": f"artifact not found: {name}"}


@mcp.tool()
def resume_task(
    task_id: str = Field(description="Task to resume after crash/timeout"),
) -> dict[str, Any]:
    """Idempotently resume a task. COMPLETED stages are never re-run; Codex
    budget is never re-spent; no duplicate commits/PRs/Windows-syncs. Re-enters
    run_next_stage for the current (incomplete) stage."""
    try:
        return _gateway.resume_task(task_id)
    except PolicyError as e:
        return {"error": str(e), "task_id": task_id}


def main() -> int:
    """Main entry point for the stdio MCP server."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
