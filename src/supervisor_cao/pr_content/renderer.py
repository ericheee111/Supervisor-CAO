"""Canonical PR content renderer.

Generates a forge-agnostic PR content package (json + md + sha256) from local
artifacts. NEVER accesses the network, forge APIs, or gh. Pure functions only.

The package is a pure function of (artifacts, task_id, branches, push_evidence)
and is byte-identical on repeated calls (idempotent).

Output conventions:
  - UTF-8, LF, trailing newline
  - Fixed JSON key order (deterministic)
  - No generated_at, no rendered_sha256 (avoids circular dependency)
  - workflow_state is always "PR_CONTENT_READY"
  - title is the bare task_id (no forced [Draft] prefix)
  - sha256 file is two lines: <json-hash>  pr-content.json / <md-hash>  pr-content.md
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1

# Fixed JSON key order for deterministic output.
_JSON_KEYS = [
    "schema_version", "task_id", "title", "base_branch", "head_branch",
    "workflow_state", "candidate_sha", "tested_sha", "reviewed_sha",
    "changed_files", "plan_summary", "local_verification", "remote_verification",
    "review_decision", "review_findings", "codex_call_count", "codex_budget",
    "known_risks", "artifact_paths",
]


@dataclass
class PRContentSchema:
    schema_version: int
    task_id: str
    title: str
    base_branch: str
    head_branch: str
    workflow_state: str
    candidate_sha: str
    tested_sha: str
    reviewed_sha: str
    changed_files: list[str]
    plan_summary: str
    local_verification: dict
    remote_verification: dict
    review_decision: str
    review_findings: list
    codex_call_count: int
    codex_budget: dict
    known_risks: list[str]
    artifact_paths: list[str]


def _extract_plan_summary(plan: dict) -> str:
    """Extract a short plan summary string from the plan artifact."""
    steps = plan.get("steps", plan.get("description", ""))
    if isinstance(steps, list):
        parts = []
        for s in steps[:5]:
            if isinstance(s, dict):
                parts.append(str(s.get("description", s)))
            else:
                parts.append(str(s))
        return "; ".join(parts)
    return str(steps)


def _validate(artifacts: dict, push_evidence: dict, task_id: str,
              head_branch: str) -> str:
    """Validate all preconditions. Returns candidate_sha. Raises ValueError."""
    for name in ["plan", "implementation", "verification", "review", "budget"]:
        if name not in artifacts:
            raise ValueError(f"missing artifact: {name}")
    impl = artifacts["implementation"]
    ver = artifacts["verification"]
    rev = artifacts["review"]
    candidate = impl.get("candidate_sha") or ver.get("candidate_sha") or ""
    if not candidate:
        raise ValueError("cannot determine candidate_sha from artifacts")
    tested = ver.get("tested_sha", "")
    reviewed = rev.get("reviewed_sha", "")
    if tested and tested != candidate:
        raise ValueError(
            f"SHA mismatch: tested_sha={tested} != candidate={candidate}")
    if reviewed and reviewed != candidate:
        raise ValueError(
            f"SHA mismatch: reviewed_sha={reviewed} != candidate={candidate}")
    if rev.get("decision") and rev["decision"] != "APPROVED":
        raise ValueError(f"review not APPROVED: {rev.get('decision')}")
    # push evidence
    if not push_evidence:
        raise ValueError("missing push evidence (push.json)")
    if not push_evidence.get("push_succeeded"):
        raise ValueError("push did not succeed (push_succeeded != true)")
    pushed = push_evidence.get("pushed_sha", "")
    if pushed != candidate:
        raise ValueError(
            f"push SHA mismatch: pushed_sha={pushed} != candidate={candidate}")
    if push_evidence.get("branch") != head_branch:
        raise ValueError(
            f"push branch mismatch: {push_evidence.get('branch')} != {head_branch}")
    return candidate


def render_pr_content(artifacts: dict, task_id: str, base_branch: str,
                      head_branch: str, push_evidence: dict) -> tuple[str, str, str]:
    """Render the PR content package.

    Returns (json_text, md_text, sha256_text).
    Pure function: no network, no side effects. Idempotent.
    """
    candidate = _validate(artifacts, push_evidence, task_id, head_branch)
    impl = artifacts["implementation"]
    ver = artifacts["verification"]
    rev = artifacts["review"]
    budget = artifacts["budget"]

    data = PRContentSchema(
        schema_version=SCHEMA_VERSION,
        task_id=task_id,
        title=task_id,
        base_branch=base_branch,
        head_branch=head_branch,
        workflow_state="PR_CONTENT_READY",
        candidate_sha=candidate,
        tested_sha=ver.get("tested_sha", candidate),
        reviewed_sha=rev.get("reviewed_sha", candidate),
        changed_files=impl.get("changed_files", []),
        plan_summary=_extract_plan_summary(artifacts["plan"]),
        local_verification=ver.get("wsl_results", {}),
        remote_verification=ver.get("remote_results", {}),
        review_decision=rev.get("decision", "APPROVED"),
        review_findings=rev.get("findings", []),
        codex_call_count=budget.get("total_used", 0),
        codex_budget=budget,
        known_risks=[],
        artifact_paths=["plan.json", "implementation.json", "verification.json",
                        "review.json", "codex-budget-summary.json"],
    )
    json_obj = {k: getattr(data, k) for k in _JSON_KEYS}
    json_text = json.dumps(json_obj, indent=2, ensure_ascii=False) + "\n"
    md_text = _render_markdown(data)
    sha_text = compute_checksums(json_text, md_text)
    return json_text, md_text, sha_text


def _render_markdown(data: PRContentSchema) -> str:
    """Render the human-readable Markdown PR body."""
    lines = [
        f"# {data.task_id}",
        "",
        f"**Workflow state:** `{data.workflow_state}`",
        "",
        f"**Base:** `{data.base_branch}`  ",
        f"**Head:** `{data.head_branch}`",
        "",
        "## SHAs",
        f"- candidate: `{data.candidate_sha}`",
        f"- tested: `{data.tested_sha}`",
        f"- reviewed: `{data.reviewed_sha}`",
        "",
        "## Plan summary",
        data.plan_summary,
        "",
        "## Changed files",
        *[f"- `{f}`" for f in data.changed_files],
        "",
        "## Local verification",
        "```json",
        json.dumps(data.local_verification, indent=2, default=str),
        "```",
        "",
        "## Remote verification",
        "```json",
        json.dumps(data.remote_verification, indent=2, default=str),
        "```",
        "",
        "## Review",
        f"- decision: `{data.review_decision}`",
        "```json",
        json.dumps(data.review_findings, indent=2, default=str),
        "```",
        "",
        f"**Codex calls used:** {data.codex_call_count} / 4",
        "",
        "## Artifacts",
        *[f"- `{p}`" for p in data.artifact_paths],
        "",
        "## Known risks",
        *([f"- {r}" for r in data.known_risks]
          if data.known_risks else ["- (none identified)"]),
        "",
        "---",
        "_Generated by Supervisor-CAO. Create the PR on your forge of choice._",
    ]
    return "\n".join(lines) + "\n"


def compute_checksums(json_text: str, md_text: str) -> str:
    """Compute the two-line sha256 checksum file content.

    Format:
      <json-sha256>  pr-content.json
      <markdown-sha256>  pr-content.md
    """
    j_hash = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    m_hash = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    return f"{j_hash}  pr-content.json\n{m_hash}  pr-content.md\n"
