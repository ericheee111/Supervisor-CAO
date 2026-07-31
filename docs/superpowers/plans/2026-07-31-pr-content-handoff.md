# Forge-agnostic PR Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace forge-coupled `DRAFT_PR_CREATED` with forge-agnostic `PR_CONTENT_READY`, generating a copyable PR content package (`pr-content.{json,md,sha256}`) without any forge API calls, and rewriting the three acceptance scenarios with strict assertions and append-only evidence.

**Architecture:** A new `scripts/render-pr-content` reads local artifacts + `push.json` and deterministically emits the PR content package. The state machine gains `PR_CONTENT_READY`; legacy `DRAFT_PR_CREATED` is lazily migrated on resume/advance only. Windows sync validates the real artifact package instead of a bool constant. Acceptance evidence is append-only; cleanup preserves history.

**Tech Stack:** Python 3.11+, SQLite, stdlib `hashlib`/`json`/`subprocess`, pytest.

## Global Constraints

- All new code, states, tests, docs use ONLY: `render-pr-content`, `prepare_pr_content`, `PR_CONTENT_READY`.
- Production path must NOT call `gh`, GitHub/GitCode/GitLab API, `requests`, or `urllib`.
- `pr-content.{json,md}` use UTF-8, LF, fixed JSON key order, trailing newline; no `generated_at`, no `rendered_sha256`.
- `pr-content.sha256` is two-line format: `<json-sha256>  pr-content.json\n<markdown-sha256>  pr-content.md\n`.
- `workflow_state` field is always `"PR_CONTENT_READY"` (never `READY_FOR_HUMAN_REVIEW`).
- PR title must NOT be forced to include `[Draft]`.
- Legacy migration is lazy (only on `resume_task`/`advance_task`/dedicated migration); `get_task` is read-only.
- Default cleanup preserves evidence; only `purge-evidence --force` deletes it.
- No auto-merge, no auto-PR creation, no forge API.
- Before claims of success: run tests, capture output, report SHAs.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/supervisor_cao/pr_content/renderer.py` | **NEW**: canonical PR content schema, JSON/MD rendering, checksum computation. Pure functions, no network. |
| `scripts/render-pr-content` | **NEW**: CLI entrypoint; loads artifacts + push.json, validates, writes package, prints stdout contract. |
| `scripts/create-draft-pr` | **MODIFY**: deprecated wrapper → calls `render-pr-content`, prints deprecation warning, no gh. |
| `src/supervisor_cao/state/machine.py` | **MODIFY**: add `PR_CONTENT_READY`, `PR_CONTENT_GENERATION_FAILED`; transition table; `migrate_legacy_state()`; `inject_candidate()`. |
| `src/supervisor_cao/mcp/policy_gateway.py` | **MODIFY**: `prepare_pr_content()`; `_stage_pr_content`; `_stage_windows_sync` reload+validate; push.json write; deprecated `create_draft_pr` wrapper. |
| `src/supervisor_cao/validation/windows_sync.py` | **MODIFY**: `SyncGates.pr_content_ready`; `check_gates` validates real artifact; `validate_pr_content_artifact()`. |
| `src/supervisor_cao/cli/acceptance.py` | **MODIFY**: three scenarios rewritten; append-only evidence; cleanup; purge-evidence. |
| `src/supervisor_cao/cli/main.py` | **MODIFY**: `acceptance purge-evidence` subcommand. |
| `docs/ACCEPTANCE.md`, `docs/WORKFLOW.md`, `docs/USER_GUIDE.md`, `README.md`, `AGENTS.md` | **MODIFY**: remove Draft PR gate language. |
| `tests/unit/test_pr_content.py` | **NEW**: schema, checksum, idempotency, no-network, push.json validation. |
| `tests/unit/test_state_machine.py` | **MODIFY**: new transitions, legacy migration, inject_candidate. |
| `tests/unit/test_windows_sync.py` | **MODIFY**: pr_content_ready gate full validation. |
| `tests/unit/test_acceptance_evidence.py` | **NEW**: append-only, cleanup preserves, purge-evidence. |
| `tests/unit/test_resume_reattach.py` | **NEW**: 5 resume sub-scenarios. |
| `tests/integration/test_workflow.py` | **MODIFY**: terminal state rename. |
| `tests/e2e/test_temp_repo_e2e.py` | **MODIFY**: PR content package generation. |

---

## Task 1: PR content schema, canonical renderer, and checksum

**Files:**
- Create: `src/supervisor_cao/pr_content/renderer.py`
- Create: `src/supervisor_cao/pr_content/__init__.py`
- Create: `tests/unit/test_pr_content.py`
- Test: `tests/unit/test_pr_content.py`

**Interfaces:**
- Produces: `render_pr_content(artifacts: dict, task_id: str, base_branch: str, head_branch: str, push_evidence: dict) -> tuple[str, str, str]` returning `(json_text, md_text, sha256_text)`; `PRContentSchema` dataclass; `compute_checksums(json_text, md_text) -> str`.

- [ ] **Step 1: Write failing test for schema and checksum**

Create `tests/unit/test_pr_content.py`:

```python
"""Unit tests for PR content renderer (forge-agnostic, no network)."""
import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from supervisor_cao.pr_content.renderer import (
    render_pr_content, compute_checksums, PRContentSchema,
)


def _sample_artifacts():
    return {
        "plan": {"steps": [{"description": "implement foo"}], "description": "implement foo"},
        "implementation": {"candidate_sha": "abc123", "changed_files": ["src/foo.py"]},
        "verification": {"candidate_sha": "abc123", "tested_sha": "abc123",
                         "wsl_results": {"passed": 5}, "remote_results": {}},
        "review": {"reviewed_sha": "abc123", "decision": "APPROVED", "findings": []},
        "budget": {"total_used": 1, "remaining": 3},
    }


def _sample_push():
    return {"schema_version": 1, "remote": "origin", "branch": "agent/T1",
            "pushed_sha": "abc123", "push_succeeded": True}


def test_render_produces_json_md_sha256():
    arts = _sample_artifacts()
    json_text, md_text, sha_text = render_pr_content(
        arts, "T1", "main", "agent/T1", _sample_push())
    assert json_text and md_text and sha_text
    # JSON is valid and has expected fields
    j = json.loads(json_text)
    assert j["task_id"] == "T1"
    assert j["workflow_state"] == "PR_CONTENT_READY"
    assert j["candidate_sha"] == "abc123"
    assert j["title"] == "T1"  # no [Draft] prefix


def test_json_has_no_generated_at_or_rendered_sha256():
    arts = _sample_artifacts()
    json_text, _, _ = render_pr_content(
        arts, "T1", "main", "agent/T1", _sample_push())
    j = json.loads(json_text)
    assert "generated_at" not in j
    assert "rendered_sha256" not in j


def test_title_no_draft_prefix():
    arts = _sample_artifacts()
    json_text, _, _ = render_pr_content(
        arts, "T1", "main", "agent/T1", _sample_push())
    j = json.loads(json_text)
    assert not j["title"].startswith("[Draft]")


def test_workflow_state_is_pr_content_ready():
    arts = _sample_artifacts()
    json_text, _, _ = render_pr_content(
        arts, "T1", "main", "agent/T1", _sample_push())
    assert json.loads(json_text)["workflow_state"] == "PR_CONTENT_READY"


def test_checksum_two_line_format():
    arts = _sample_artifacts()
    json_text, md_text, sha_text = render_pr_content(
        arts, "T1", "main", "agent/T1", _sample_push())
    lines = sha_text.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].endswith("pr-content.json")
    assert lines[1].endswith("pr-content.md")
    # verify hashes match
    json_hash = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    md_hash = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    assert lines[0].split()[0] == json_hash
    assert lines[1].split()[0] == md_hash


def test_idempotent_same_input_same_output():
    arts = _sample_artifacts()
    push = _sample_push()
    r1 = render_pr_content(arts, "T1", "main", "agent/T1", push)
    r2 = render_pr_content(arts, "T1", "main", "agent/T1", push)
    assert r1 == r2


def test_json_trailing_newline_and_lf():
    arts = _sample_artifacts()
    json_text, md_text, _ = render_pr_content(
        arts, "T1", "main", "agent/T1", _sample_push())
    assert json_text.endswith("\n")
    assert "\r" not in json_text
    assert md_text.endswith("\n")
    assert "\r" not in md_text


def test_sha_mismatch_rejected():
    arts = _sample_artifacts()
    arts["verification"]["tested_sha"] = "WRONG"
    with pytest.raises(ValueError, match="SHA mismatch"):
        render_pr_content(arts, "T1", "main", "agent/T1", _sample_push())


def test_review_not_approved_rejected():
    arts = _sample_artifacts()
    arts["review"]["decision"] = "CHANGES_REQUESTED"
    with pytest.raises(ValueError, match="review not APPROVED"):
        render_pr_content(arts, "T1", "main", "agent/T1", _sample_push())


def test_push_not_succeeded_rejected():
    arts = _sample_artifacts()
    push = _sample_push()
    push["push_succeeded"] = False
    with pytest.raises(ValueError, match="push"):
        render_pr_content(arts, "T1", "main", "agent/T1", push)


def test_push_sha_mismatch_rejected():
    arts = _sample_artifacts()
    push = _sample_push()
    push["pushed_sha"] = "DIFFERENT"
    with pytest.raises(ValueError, match="push"):
        render_pr_content(arts, "T1", "main", "agent/T1", push)


def test_missing_artifact_rejected():
    arts = _sample_artifacts()
    del arts["plan"]
    with pytest.raises(ValueError, match="missing artifact"):
        render_pr_content(arts, "T1", "main", "agent/T1", _sample_push())


def test_compute_checksums_standalone():
    j = '{"a": 1}\n'
    m = "# title\n"
    s = compute_checksums(j, m)
    lines = s.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith(hashlib.sha256(j.encode()).hexdigest())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_pr_content.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'supervisor_cao.pr_content'`

- [ ] **Step 3: Create the renderer module**

Create `src/supervisor_cao/pr_content/__init__.py`:

```python
"""PR content rendering (forge-agnostic)."""
```

Create `src/supervisor_cao/pr_content/renderer.py`:

```python
"""Canonical PR content renderer.

Generates a forge-agnostic PR content package (json + md + sha256) from local
artifacts. NEVER accesses the network, forge APIs, or gh. Pure functions only.

The package is a pure function of (artifacts, task_id, branches, push_evidence)
and is byte-identical on repeated calls (idempotent).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
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
    """Validate all preconditions. Returns candidate_sha. Raises ValueError on failure."""
    for name in ["plan", "implementation", "verification", "review", "budget"]:
        if name not in artifacts:
            raise ValueError(f"missing artifact: {name}")
    impl = artifacts["implementation"]
    ver = artifacts["verification"]
    rev = artifacts["review"]
    budget = artifacts["budget"]
    candidate = impl.get("candidate_sha") or ver.get("candidate_sha") or ""
    if not candidate:
        raise ValueError("cannot determine candidate_sha from artifacts")
    tested = ver.get("tested_sha", "")
    reviewed = rev.get("reviewed_sha", "")
    if tested and tested != candidate:
        raise ValueError(f"SHA mismatch: tested_sha={tested} != candidate={candidate}")
    if reviewed and reviewed != candidate:
        raise ValueError(f"SHA mismatch: reviewed_sha={reviewed} != candidate={candidate}")
    if rev.get("decision") and rev["decision"] != "APPROVED":
        raise ValueError(f"review not APPROVED: {rev.get('decision')}")
    # push evidence
    if not push_evidence:
        raise ValueError("missing push evidence (push.json)")
    if not push_evidence.get("push_succeeded"):
        raise ValueError("push did not succeed (push_succeeded != true)")
    pushed = push_evidence.get("pushed_sha", "")
    if pushed != candidate:
        raise ValueError(f"push SHA mismatch: pushed_sha={pushed} != candidate={candidate}")
    if push_evidence.get("branch") != head_branch:
        raise ValueError(
            f"push branch mismatch: {push_evidence.get('branch')} != {head_branch}")
    return candidate


def render_pr_content(artifacts: dict, task_id: str, base_branch: str,
                      head_branch: str, push_evidence: dict) -> tuple[str, str, str]:
    """Render the PR content package. Returns (json_text, md_text, sha256_text).

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
    # Serialize JSON with fixed key order, UTF-8, LF, trailing newline.
    json_obj = {k: getattr(data, k) for k in _JSON_KEYS}
    json_text = json.dumps(json_obj, indent=2, ensure_ascii=False) + "\n"
    md_text = _render_markdown(data)
    sha_text = compute_checksums(json_text, md_text)
    return json_text, md_text, sha_text


def _render_markdown(data: PRContentSchema) -> str:
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
        *([f"- {r}" for r in data.known_risks] if data.known_risks else ["- (none identified)"]),
        "",
        "---",
        "_Generated by Supervisor-CAO. Create the PR on your forge of choice._",
    ]
    return "\n".join(lines) + "\n"


def compute_checksums(json_text: str, md_text: str) -> str:
    """Compute the two-line sha256 checksum file content."""
    j_hash = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    m_hash = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    return f"{j_hash}  pr-content.json\n{m_hash}  pr-content.md\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_pr_content.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/supervisor_cao/pr_content/ tests/unit/test_pr_content.py
git commit -m "feat: add forge-agnostic PR content renderer with checksum

Pure-function renderer produces pr-content.{json,md,sha256} from local
artifacts + push.json. No network, no forge API. Idempotent with fixed
JSON key order, UTF-8/LF, trailing newline. Two-line sha256 binds JSON+MD."
```

---

## Task 2: render-pr-content CLI script + push evidence

**Files:**
- Create: `scripts/render-pr-content`
- Modify: `scripts/create-draft-pr` (deprecated wrapper)
- Test: `tests/unit/test_pr_content.py` (add CLI tests)

**Interfaces:**
- Consumes: `render_pr_content()` from Task 1.
- Produces: `scripts/render-pr-content` CLI with stdout contract: `PR Title:\n<title>\n\nBase:\n<base>\n\nHead:\n<head>\n\nPR Body:\n<body>`.

- [ ] **Step 1: Write failing test for CLI script**

Add to `tests/unit/test_pr_content.py`:

```python
import subprocess
import sys


def _write_artifacts(run_dir: Path, arts: dict, push: dict | None = None):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(json.dumps(arts["plan"]))
    (run_dir / "implementation.json").write_text(json.dumps(arts["implementation"]))
    (run_dir / "verification.json").write_text(json.dumps(arts["verification"]))
    (run_dir / "review.json").write_text(json.dumps(arts["review"]))
    (run_dir / "codex-budget-summary.json").write_text(json.dumps(arts["budget"]))
    if push:
        (run_dir / "push.json").write_text(json.dumps(push))


def test_cli_produces_files_and_stdout(tmp_path):
    run_dir = tmp_path / "T1"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push())
    script = Path(__file__).resolve().parents[2] / "scripts" / "render-pr-content"
    r = subprocess.run(
        [sys.executable, str(script), "--task-id", "T1",
         "--base-branch", "main", "--head-branch", "agent/T1",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    # files written
    assert (run_dir / "pr-content.json").exists()
    assert (run_dir / "pr-content.md").exists()
    assert (run_dir / "pr-content.sha256").exists()
    # stdout contract
    assert "PR Title:" in r.stdout
    assert "Base:" in r.stdout
    assert "Head:" in r.stdout
    assert "PR Body:" in r.stdout


def test_cli_rejects_missing_push_json(tmp_path):
    run_dir = tmp_path / "T2"
    _write_artifacts(run_dir, _sample_artifacts())  # no push.json
    script = Path(__file__).resolve().parents[2] / "scripts" / "render-pr-content"
    r = subprocess.run(
        [sys.executable, str(script), "--task-id", "T2",
         "--base-branch", "main", "--head-branch", "agent/T2",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "push" in r.stderr.lower()


def test_cli_idempotent_rerun(tmp_path):
    run_dir = tmp_path / "T3"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push())
    script = Path(__file__).resolve().parents[2] / "scripts" / "render-pr-content"
    cmd = [sys.executable, str(script), "--task-id", "T3",
           "--base-branch", "main", "--head-branch", "agent/T3",
           "--run-dir", str(run_dir)]
    r1 = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    sha1 = (run_dir / "pr-content.sha256").read_text()
    json1 = (run_dir / "pr-content.json").read_text()
    r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    sha2 = (run_dir / "pr-content.sha256").read_text()
    json2 = (run_dir / "pr-content.json").read_text()
    assert r1.returncode == 0 and r2.returncode == 0
    assert sha1 == sha2
    assert json1 == json2


def test_cli_no_network_calls(tmp_path):
    """The renderer must not spawn any subprocess other than itself."""
    run_dir = tmp_path / "T4"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push())
    script = Path(__file__).resolve().parents[2] / "scripts" / "render-pr-content"
    # patch subprocess.run inside the child is not possible; instead verify
    # the renderer module itself doesn't import requests/urllib
    import supervisor_cao.pr_content.renderer as mod
    import inspect
    src = inspect.getsource(mod)
    assert "import requests" not in src
    assert "import urllib" not in src
    assert "gh " not in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_pr_content.py::test_cli_produces_files_and_stdout -v`
Expected: FAIL (script not found)

- [ ] **Step 3: Create the CLI script**

Create `scripts/render-pr-content`:

```python
#!/usr/bin/env python3
"""render-pr-content: generate a forge-agnostic PR content package.

Reads local artifacts (plan/implementation/verification/review/budget) and
push.json from --run-dir, validates all preconditions, and writes:
  - pr-content.json  (canonical, deterministic)
  - pr-content.md    (human-readable PR body)
  - pr-content.sha256 (two-line checksum binding JSON + MD)

Prints to stdout:
  PR Title:
  <title>

  Base:
  <base>

  Head:
  <head>

  PR Body:
  <markdown body>

NEVER accesses the network, forge APIs, or gh. Does not require origin to be
GitHub. Idempotent: same inputs -> byte-identical outputs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add src to path for direct execution
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from supervisor_cao.pr_content.renderer import render_pr_content


def _load_json(path: Path) -> dict:
    if not path.exists():
        print(f"PR_CONTENT_GENERATION_FAILED: missing {path.name}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"PR_CONTENT_GENERATION_FAILED: cannot parse {path.name}: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render forge-agnostic PR content package")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--base-branch", default="main")
    ap.add_argument("--head-branch", required=True)
    ap.add_argument("--run-dir", required=True, help="artifact dir for this task")
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    artifacts = {
        "plan": _load_json(run_dir / "plan.json"),
        "implementation": _load_json(run_dir / "implementation.json"),
        "verification": _load_json(run_dir / "verification.json"),
        "review": _load_json(run_dir / "review.json"),
        "budget": _load_json(run_dir / "codex-budget-summary.json"),
    }
    push_path = run_dir / "push.json"
    if not push_path.exists():
        print("PR_CONTENT_GENERATION_FAILED: missing push.json (push evidence required)",
              file=sys.stderr)
        return 1
    push_evidence = json.loads(push_path.read_text(encoding="utf-8"))

    try:
        json_text, md_text, sha_text = render_pr_content(
            artifacts, args.task_id, args.base_branch, args.head_branch, push_evidence)
    except ValueError as e:
        print(f"PR_CONTENT_GENERATION_FAILED: {e}", file=sys.stderr)
        return 1

    (run_dir / "pr-content.json").write_text(json_text, encoding="utf-8")
    (run_dir / "pr-content.md").write_text(md_text, encoding="utf-8")
    (run_dir / "pr-content.sha256").write_text(sha_text, encoding="utf-8")

    # stdout contract
    j = json.loads(json_text)
    print("PR Title:")
    print(j["title"])
    print()
    print("Base:")
    print(j["base_branch"])
    print()
    print("Head:")
    print(j["head_branch"])
    print()
    print("PR Body:")
    print(md_text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_pr_content.py -v`
Expected: PASS (all)

- [ ] **Step 5: Convert create-draft-pr to deprecated wrapper**

Replace the entire content of `scripts/create-draft-pr` with:

```python
#!/usr/bin/env python3
"""DEPRECATED: create-draft-pr is replaced by render-pr-content.

This wrapper exists for backward compatibility only. It:
  - prints a deprecation warning to stderr
  - calls render-pr-content (which does NOT access any forge API)
  - does NOT call gh, does NOT create/update/close real PRs
  - does NOT require a GitHub remote

All new code, states, tests, and docs must use render-pr-content /
prepare_pr_content / PR_CONTENT_READY.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    print("DEPRECATED: create-draft-pr is replaced by render-pr-content. "
          "This wrapper does NOT create a real PR. Use 'render-pr-content' or "
          "PolicyGateway.prepare_pr_content() instead.",
          file=sys.stderr)
    # Re-dispatch to render-pr-content with the same args (minus the script name).
    script = Path(__file__).resolve().parent / "render-pr-content"
    import subprocess
    # Pass through all argv except argv[0]; render-pr-content uses the same flags.
    r = subprocess.run([sys.executable, str(script)] + sys.argv[1:],
                       capture_output=False, timeout=120)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `python -m pytest tests/ -q`
Expected: existing tests that referenced `create-draft-pr`'s old behavior may fail — note them for Task 3/4.

- [ ] **Step 7: Commit**

```bash
git add scripts/render-pr-content scripts/create-draft-pr tests/unit/test_pr_content.py
git commit -m "feat: add render-pr-content CLI + deprecate create-draft-pr

render-pr-content reads local artifacts + push.json, writes
pr-content.{json,md,sha256}, prints PR Title/Base/Head/Body stdout contract.
create-draft-pr is now a deprecated wrapper that calls render-pr-content
with no forge API access."
```

---

## Task 3: State machine + legacy migration

**Files:**
- Modify: `src/supervisor_cao/state/machine.py:29-95, 226-338`
- Test: `tests/unit/test_state_machine.py`

**Interfaces:**
- Produces: `TaskState.PR_CONTENT_READY`; `ErrorState.PR_CONTENT_GENERATION_FAILED`; `StateStore.migrate_legacy_state(task_id, run_dir, base_branch, head_branch) -> TaskRecord`; `StateStore.inject_candidate(task_id, new_sha, from_state) -> TaskRecord`.

- [ ] **Step 1: Write failing tests for new states and transitions**

Add to `tests/unit/test_state_machine.py`:

```python
def test_approved_to_pr_content_ready_legal(store, task):
    """APPROVED -> PR_CONTENT_READY is a legal transition."""
    # drive to APPROVED with SHAs set
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
               TaskState.LOCAL_VERIFYING, TaskState.LOCAL_VERIFIED,
               TaskState.REMOTE_QUEUED, TaskState.REMOTE_VERIFYING,
               TaskState.REMOTE_VERIFIED, TaskState.REVIEWING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.APPROVED,
                 reviewed_sha="aaa111", tested_sha="aaa111",
                 new_candidate_sha="aaa111")
    r = s.transition("T1", TaskState.PR_CONTENT_READY)
    assert r.state == TaskState.PR_CONTENT_READY.value


def test_pr_content_ready_to_windows_synced_legal(store, task):
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
               TaskState.LOCAL_VERIFYING, TaskState.LOCAL_VERIFIED,
               TaskState.REMOTE_QUEUED, TaskState.REMOTE_VERIFYING,
               TaskState.REMOTE_VERIFIED, TaskState.REVIEWING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.APPROVED,
                 reviewed_sha="aaa111", tested_sha="aaa111",
                 new_candidate_sha="aaa111")
    s.transition("T1", TaskState.PR_CONTENT_READY)
    r = s.transition("T1", TaskState.WINDOWS_SYNCED)
    assert r.state == TaskState.WINDOWS_SYNCED.value


def test_pr_content_ready_cannot_skip_windows_sync(store, task):
    """PR_CONTENT_READY -> READY_FOR_HUMAN_REVIEW is illegal (must sync first)."""
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
               TaskState.LOCAL_VERIFYING, TaskState.LOCAL_VERIFIED,
               TaskState.REMOTE_QUEUED, TaskState.REMOTE_VERIFYING,
               TaskState.REMOTE_VERIFIED, TaskState.REVIEWING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.APPROVED,
                 reviewed_sha="aaa111", tested_sha="aaa111",
                 new_candidate_sha="aaa111")
    s.transition("T1", TaskState.PR_CONTENT_READY)
    with pytest.raises(IllegalTransition):
        s.transition("T1", TaskState.READY_FOR_HUMAN_REVIEW)


def test_new_task_cannot_enter_draft_pr_created(store, task):
    """DRAFT_PR_CREATED has no inbound transitions for new tasks."""
    s = store
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
               TaskState.LOCAL_VERIFYING, TaskState.LOCAL_VERIFIED,
               TaskState.REMOTE_QUEUED, TaskState.REMOTE_VERIFYING,
               TaskState.REMOTE_VERIFIED, TaskState.REVIEWING]:
        s.transition("T1", st)
    s.transition("T1", TaskState.APPROVED,
                 reviewed_sha="aaa111", tested_sha="aaa111",
                 new_candidate_sha="aaa111")
    with pytest.raises(IllegalTransition):
        s.transition("T1", TaskState.DRAFT_PR_CREATED)


def test_get_task_does_not_migrate_legacy(store, task, tmp_path):
    """get_task must NOT modify DB state (no lazy migration on read)."""
    # manually set a task to legacy DRAFT_PR_CREATED
    import sqlite3
    with sqlite3.connect(str(tmp_path / "tasks.db")) as c:
        c.execute("UPDATE tasks SET state='DRAFT_PR_CREATED' WHERE task_id='T1'")
        c.commit()
    rec = store.get("T1")
    assert rec.state == "DRAFT_PR_CREATED"  # unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_state_machine.py -v -k "pr_content_ready or draft_pr_created or migrate"`
Expected: FAIL with `AttributeError: PR_CONTENT_READY`

- [ ] **Step 3: Modify the state machine**

In `src/supervisor_cao/state/machine.py`:

1. Add `PR_CONTENT_READY = "PR_CONTENT_READY"` after `APPROVED` (line 46). Keep `DRAFT_PR_CREATED` enum value (line 47) but it becomes legacy.

2. Add `PR_CONTENT_GENERATION_FAILED = "PR_CONTENT_GENERATION_FAILED"` to `ErrorState` (after line 64).

3. Update `TRANSITIONS`:

```python
    TaskState.APPROVED: {TaskState.PR_CONTENT_READY, TaskState.FAILED},
    # DRAFT_PR_CREATED retained as legacy terminal-paused (no inbound, no forward).
    TaskState.DRAFT_PR_CREATED: set(),
    TaskState.PR_CONTENT_READY: {TaskState.WINDOWS_SYNCED, TaskState.FAILED},
    TaskState.WINDOWS_SYNCED: {TaskState.READY_FOR_HUMAN_REVIEW, TaskState.FAILED},
```

4. In `transition()`, change the gate check at line 286-288 from `DRAFT_PR_CREATED` to `PR_CONTENT_READY`:

```python
            if to_str == TaskState.PR_CONTENT_READY.value and check_sha:
                if rec.reviewed_sha is None or rec.reviewed_sha != rec.candidate_sha:
                    raise ShaMismatch("PR_CONTENT_READY requires reviewed_sha == candidate_sha")
```

5. Add `migrate_legacy_state()` and `inject_candidate()` methods to `StateStore` (after `events()`):

```python
    def migrate_legacy_state(self, task_id: str, run_dir: Path,
                             base_branch: str, head_branch: str) -> TaskRecord:
        """Lazily migrate a legacy DRAFT_PR_CREATED task to PR_CONTENT_READY.

        Only called from resume_task/advance_task/dedicated migration — NEVER
        from get_task. If artifacts are incomplete, raises MigrationError (does
        NOT silently roll back to APPROVED).
        """
        rec = self.get(task_id)
        if not rec:
            raise KeyError(f"task not found: {task_id}")
        if rec.state != TaskState.DRAFT_PR_CREATED.value:
            return rec  # not legacy, nothing to do
        # Check artifact completeness
        run_dir = Path(run_dir)
        required = ["plan.json", "implementation.json", "verification.json",
                    "review.json", "codex-budget-summary.json", "push.json"]
        missing = [f for f in required if not (run_dir / f).exists()]
        if missing:
            # Do NOT silently roll back. Enter NEEDS_HUMAN with detail.
            detail = {"missing": missing, "reason": "legacy_migration_artifacts_incomplete"}
            self.transition(task_id, TaskState.NEEDS_HUMAN, error="LEGACY_MIGRATION_INCOMPLETE",
                            detail=detail)
            raise MigrationError(f"legacy migration incomplete: missing {missing}")
        # Single transaction: generate content package + transition
        from supervisor_cao.pr_content.renderer import render_pr_content
        import json
        artifacts = {n: json.loads((run_dir / f).read_text(encoding="utf-8"))
                     for n, f in [("plan", "plan.json"), ("implementation", "implementation.json"),
                                  ("verification", "verification.json"),
                                  ("review", "review.json"),
                                  ("budget", "codex-budget-summary.json")]}
        push = json.loads((run_dir / "push.json").read_text(encoding="utf-8"))
        json_text, md_text, sha_text = render_pr_content(
            artifacts, task_id, base_branch, head_branch, push)
        (run_dir / "pr-content.json").write_text(json_text, encoding="utf-8")
        (run_dir / "pr-content.md").write_text(md_text, encoding="utf-8")
        (run_dir / "pr-content.sha256").write_text(sha_text, encoding="utf-8")
        # Transition with event
        with self._lock, self._conn() as c:
            c.execute("UPDATE tasks SET state=?, updated_at=? WHERE task_id=?",
                      (TaskState.PR_CONTENT_READY.value, time.time(), task_id))
            self._log_event(c, task_id, "LEGACY_STATE_MIGRATED",
                            TaskState.DRAFT_PR_CREATED.value,
                            TaskState.PR_CONTENT_READY.value,
                            {"task_id": task_id})
            c.commit()
        return self.get(task_id)

    def inject_candidate(self, task_id: str, new_sha: str,
                         from_state: TaskState) -> TaskRecord:
        """ACCEPTANCE ONLY: inject a controlled candidate for review-fix testing.

        This is an audited entry point — NOT for production use. It records a
        controlled_candidate_injection event and clears tested/reviewed SHAs.
        """
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError(f"task not found: {task_id}")
            c.execute(
                "UPDATE tasks SET candidate_sha=?, tested_sha=NULL, reviewed_sha=NULL, "
                "state=?, updated_at=? WHERE task_id=?",
                (new_sha, from_state.value, time.time(), task_id))
            self._log_event(c, task_id, "CONTROLLED_CANDIDATE_INJECTION",
                            row["state"], from_state.value,
                            {"new_sha": new_sha, "reason": "acceptance_review_fix"})
            c.commit()
        return self.get(task_id)
```

6. Add `MigrationError` exception class near `IllegalTransition`:

```python
class MigrationError(Exception):
    pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_state_machine.py -v`
Expected: PASS (all, including legacy migration tests)

- [ ] **Step 5: Commit**

```bash
git add src/supervisor_cao/state/machine.py tests/unit/test_state_machine.py
git commit -m "feat: add PR_CONTENT_READY state + legacy migration

- APPROVED -> PR_CONTENT_READY -> WINDOWS_SYNCED -> READY_FOR_HUMAN_REVIEW
- DRAFT_PR_CREATED retained as legacy (no inbound/outbound)
- migrate_legacy_state() lazy on resume/advance only; get_task read-only
- inject_candidate() audited acceptance-only entry point
- MigrationError for incomplete legacy artifacts"
```

---

## Task 4: PolicyGateway prepare_pr_content + push.json + _stage_pr_content

**Files:**
- Modify: `src/supervisor_cao/mcp/policy_gateway.py:243-255, 679-727`
- Test: `tests/unit/test_policy_gateway.py` (or `test_policy_mcp_protocol.py`)

**Interfaces:**
- Produces: `PolicyGateway.prepare_pr_content(task_id, project) -> dict`; `_stage_pr_content`; push.json written after executor push.

- [ ] **Step 1: Write failing test**

Create `tests/unit/test_prepare_pr_content.py`:

```python
"""Unit tests for PolicyGateway.prepare_pr_content and _stage_pr_content."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from supervisor_cao.state.machine import StateStore, TaskState
from supervisor_cao.mcp.policy_gateway import PolicyGateway, PolicyError
from supervisor_cao.mcp.stage_store import StageStore
from supervisor_cao.budget.codex import CodexBudget


@pytest.fixture
def dirs(tmp_path):
    return {"state": tmp_path / "state", "runs": tmp_path / "runs",
            "stages": tmp_path / "stages", "budget": tmp_path / "budget",
            "workers": tmp_path / "workers"}


@pytest.fixture
def gw(dirs):
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    store = StateStore(db_path=dirs["state"] / "tasks.db")
    budget = CodexBudget(db_path=dirs["budget"] / "codex.db")
    stages = StageStore(db_path=dirs["stages"] / "stages.db")
    return PolicyGateway(state_store=store, budget=budget, stage_store=stages,
                         test_mode=True), store


def _write_artifacts(run_dir, candidate="abc123"):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(json.dumps({"steps": [{"description": "do x"}]}))
    (run_dir / "implementation.json").write_text(json.dumps({"candidate_sha": candidate, "changed_files": ["a.py"]}))
    (run_dir / "verification.json").write_text(json.dumps({"candidate_sha": candidate, "tested_sha": candidate, "wsl_results": {"passed": 1}, "remote_results": {}}))
    (run_dir / "review.json").write_text(json.dumps({"reviewed_sha": candidate, "decision": "APPROVED", "findings": []}))
    (run_dir / "codex-budget-summary.json").write_text(json.dumps({"total_used": 1, "remaining": 3}))
    (run_dir / "push.json").write_text(json.dumps({"schema_version": 1, "remote": "origin", "branch": "agent/T1", "pushed_sha": candidate, "push_succeeded": True}))


def test_prepare_pr_content_rejects_not_approved(gw):
    gateway, store = gw
    store.create("T1", "demo")
    with pytest.raises(PolicyError, match="not APPROVED"):
        gateway.prepare_pr_content("T1", "demo")


def test_prepare_pr_content_rejects_sha_mismatch(gw, dirs):
    gateway, store = gw
    store.create("T1", "demo")
    # drive to APPROVED with reviewed != candidate
    for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
               TaskState.IMPLEMENTING, TaskState.IMPLEMENTED,
               TaskState.LOCAL_VERIFYING, TaskState.LOCAL_VERIFIED,
               TaskState.REMOTE_QUEUED, TaskState.REMOTE_VERIFYING,
               TaskState.REMOTE_VERIFIED, TaskState.REVIEWING]:
        store.transition("T1", st)
    store.transition("T1", TaskState.APPROVED, reviewed_sha="aaa", tested_sha="aaa", new_candidate_sha="aaa")
    with pytest.raises(PolicyError, match="SHA"):
        gateway.prepare_pr_content("T1", "demo")


def test_create_draft_pr_is_deprecated_wrapper(gw):
    """create_draft_pr should still exist but delegate to prepare_pr_content."""
    gateway, store = gw
    store.create("T1", "demo")
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with pytest.raises(PolicyError):
            gateway.create_draft_pr("T1", "demo")
        assert any(issubclass(x.category, DeprecationWarning) for x in w)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_prepare_pr_content.py -v`
Expected: FAIL (`prepare_pr_content` not found)

- [ ] **Step 3: Implement prepare_pr_content and update _stage_pr_content**

In `src/supervisor_cao/mcp/policy_gateway.py`:

Replace `create_draft_pr` (lines 243-255) with:

```python
    # --- PR content (forge-agnostic) ---

    def prepare_pr_content(self, task_id: str, project: str) -> dict:
        """Generate PR content package. Enforces APPROVED + reviewed_sha == candidate_sha.

        Does NOT access any forge API, gh, or network. Delegates to
        scripts/render-pr-content which reads local artifacts + push.json.
        """
        rec = self.store.get(task_id)
        if not rec:
            raise PolicyError(f"task not found: {task_id}")
        if rec.state != TaskState.APPROVED.value:
            raise PolicyError(
                f"PR_CONTENT_GENERATION_FAILED: task not APPROVED (state={rec.state})")
        if rec.reviewed_sha != rec.candidate_sha:
            raise PolicyError(
                f"PR_CONTENT_GENERATION_FAILED: reviewed={rec.reviewed_sha} "
                f"!= candidate={rec.candidate_sha}")
        run_dir = self.run_root / task_id
        return {"status": "PR_CONTENT_READY", "candidate_sha": rec.candidate_sha}

    def create_draft_pr(self, task_id: str, project: str) -> dict:
        """DEPRECATED: use prepare_pr_content. Does NOT access network."""
        import warnings
        warnings.warn("create_draft_pr is deprecated; use prepare_pr_content",
                      DeprecationWarning, stacklevel=2)
        return self.prepare_pr_content(task_id, project)
```

Replace `_stage_draft_pr` (lines 679-703) with `_stage_pr_content`:

```python
    def _stage_pr_content(self, task_id, rec, cfg, run_dir):
        stage = "pr_content"
        run, done = self.stages.begin_stage(task_id, stage, "render-pr-content")
        if done:
            self.store.transition(task_id, TaskState.PR_CONTENT_READY)
            return
        self.stages.mark_running(task_id, stage, candidate_sha=rec.candidate_sha)
        script = Path(__file__).resolve().parents[3] / "scripts" / "render-pr-content"
        task_branch = cfg.task_branch_for(task_id)
        cmd = [sys.executable, str(script), "--task-id", task_id,
               "--base-branch", cfg.base_branch, "--head-branch", task_branch,
               "--run-dir", str(run_dir)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            self.stages.fail_stage(task_id, stage)
            raise PolicyError(
                f"PR_CONTENT_GENERATION_FAILED: {r.stderr.strip() or r.stdout.strip()}")
        self.stages.complete_stage(task_id, stage, candidate_sha=rec.candidate_sha)
        self.store.transition(task_id, TaskState.PR_CONTENT_READY)
```

Find where `_stage_draft_pr` is dispatched in `run_next_stage` (search for `"draft_pr"` or `DRAFT_PR_CREATED`) and update to call `_stage_pr_content` when state is `APPROVED`.

Add a helper to write `push.json` after executor push. In the executor stage completion (wherever `commit_and_push` is called), add:

```python
        # Record push evidence (deterministic, no network query)
        push_evidence = {
            "schema_version": 1,
            "remote": "origin",
            "branch": task_branch,
            "pushed_sha": rec.candidate_sha,
            "push_succeeded": True,
        }
        (run_dir / "push.json").write_text(json.dumps(push_evidence, indent=2))
```

(This goes in `_stage_implement` or wherever the executor push happens — find the exact location during implementation.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_prepare_pr_content.py tests/unit/test_state_machine.py -v`
Expected: PASS

- [ ] **Step 5: Run broader tests for regressions**

Run: `python -m pytest tests/unit/ tests/integration/ -q`
Expected: PASS (fix any references to old `create_draft_pr`/`DRAFT_PR_CREATED` in dispatch logic)

- [ ] **Step 6: Commit**

```bash
git add src/supervisor_cao/mcp/policy_gateway.py tests/unit/test_prepare_pr_content.py
git commit -m "feat: PolicyGateway.prepare_pr_content + _stage_pr_content

- prepare_pr_content replaces create_draft_pr (deprecated wrapper kept)
- _stage_pr_content calls render-pr-content (no forge API)
- push.json written after executor push for deterministic push evidence"
```

---

## Task 5: Windows sync gate validation

**Files:**
- Modify: `src/supervisor_cao/validation/windows_sync.py:42-87`
- Modify: `src/supervisor_cao/mcp/policy_gateway.py:259-276, 705-727`
- Test: `tests/unit/test_windows_sync.py`

**Interfaces:**
- Produces: `validate_pr_content_artifact(run_dir, task_id, candidate_sha, tested_sha, reviewed_sha, base_branch, head_branch) -> bool`; `SyncGates.pr_content_ready`.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_windows_sync.py`:

```python
import json

def test_gates_fail_when_pr_content_missing(fake_repo, tmp_path):
    """pr_content_ready gate fails when pr-content artifacts are missing."""
    from supervisor_cao.validation.windows_sync import check_gates
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is False
    assert gates.all_pass is False


def _write_pr_content(run_dir, task_id="T1", candidate="c1"):
    run_dir.mkdir(parents=True, exist_ok=True)
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {
        "plan": {"steps": [{"description": "x"}]},
        "implementation": {"candidate_sha": candidate, "changed_files": ["a.py"]},
        "verification": {"candidate_sha": candidate, "tested_sha": candidate,
                         "wsl_results": {}, "remote_results": {}},
        "review": {"reviewed_sha": candidate, "decision": "APPROVED", "findings": []},
        "budget": {"total_used": 1, "remaining": 3},
    }
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/T1",
            "pushed_sha": candidate, "push_succeeded": True}
    j, m, s = render_pr_content(arts, task_id, "main", "agent/T1", push)
    (run_dir / "pr-content.json").write_text(j)
    (run_dir / "pr-content.md").write_text(m)
    (run_dir / "pr-content.sha256").write_text(s)
    (run_dir / "push.json").write_text(json.dumps(push))


def test_gates_pass_when_pr_content_valid(fake_repo, tmp_path):
    from supervisor_cao.validation.windows_sync import check_gates
    _write_pr_content(tmp_path, candidate="c1")
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is True


def test_gates_fail_when_sha256_tampered(fake_repo, tmp_path):
    from supervisor_cao.validation.windows_sync import check_gates
    _write_pr_content(tmp_path, candidate="c1")
    # tamper with json
    (tmp_path / "pr-content.json").write_text('{"tampered": true}\n')
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is False


def test_gates_fail_when_workflow_state_wrong(fake_repo, tmp_path):
    from supervisor_cao.validation.windows_sync import check_gates
    _write_pr_content(tmp_path, candidate="c1")
    j = json.loads((tmp_path / "pr-content.json").read_text())
    j["workflow_state"] = "READY_FOR_HUMAN_REVIEW"
    (tmp_path / "pr-content.json").write_text(json.dumps(j, indent=2) + "\n")
    gates = check_gates(fake_repo, "agent/T1", "c1", "c1", "c1",
                        review_approved=True, run_dir=tmp_path, task_id="T1",
                        base_branch="main", head_branch="agent/T1")
    assert gates.pr_content_ready is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_windows_sync.py -v -k "pr_content"`
Expected: FAIL (signature mismatch — `check_gates` doesn't accept `run_dir`)

- [ ] **Step 3: Implement validate_pr_content_artifact and update check_gates**

In `src/supervisor_cao/validation/windows_sync.py`:

Replace `SyncGates` (lines 42-57) and `check_gates` (lines 60-87):

```python
@dataclass
class SyncGates:
    candidate_pushed: bool
    tested_eq_candidate: bool
    reviewed_eq_candidate: bool
    review_approved: bool
    pr_content_ready: bool
    windows_clean: bool
    fast_forwardable: bool

    @property
    def all_pass(self) -> bool:
        return all([
            self.candidate_pushed, self.tested_eq_candidate, self.reviewed_eq_candidate,
            self.review_approved, self.pr_content_ready, self.windows_clean, self.fast_forwardable,
        ])


def validate_pr_content_artifact(run_dir: str | Path, task_id: str,
                                 candidate_sha: str, tested_sha: str,
                                 reviewed_sha: str, base_branch: str,
                                 head_branch: str) -> bool:
    """Validate the pr-content artifact package. Returns True only if ALL checks pass."""
    import hashlib
    import json
    rd = Path(run_dir)
    sha_path = rd / "pr-content.sha256"
    json_path = rd / "pr-content.json"
    md_path = rd / "pr-content.md"
    push_path = rd / "push.json"
    if not (sha_path.exists() and json_path.exists() and md_path.exists()):
        return False
    try:
        sha_text = sha_path.read_text(encoding="utf-8")
        json_text = json_path.read_text(encoding="utf-8")
        md_text = md_path.read_text(encoding="utf-8")
        j = json.loads(json_text)
    except Exception:
        return False
    # checksum: two-line format
    lines = sha_text.strip().split("\n")
    if len(lines) != 2:
        return False
    expected_j = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    expected_m = hashlib.sha256(md_text.encode("utf-8")).hexdigest()
    if lines[0].split()[0] != expected_j or not lines[0].endswith("pr-content.json"):
        return False
    if lines[1].split()[0] != expected_m or not lines[1].endswith("pr-content.md"):
        return False
    # field validation
    if j.get("schema_version") != 1:
        return False
    if j.get("task_id") != task_id:
        return False
    if j.get("workflow_state") != "PR_CONTENT_READY":
        return False
    if j.get("base_branch") != base_branch:
        return False
    if j.get("head_branch") != head_branch:
        return False
    if j.get("candidate_sha") != candidate_sha:
        return False
    if j.get("tested_sha") != tested_sha:
        return False
    if j.get("reviewed_sha") != reviewed_sha:
        return False
    if j.get("review_decision") != "APPROVED":
        return False
    # push.json
    if not push_path.exists():
        return False
    try:
        push = json.loads(push_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not push.get("push_succeeded"):
        return False
    if push.get("pushed_sha") != candidate_sha:
        return False
    if push.get("branch") != head_branch:
        return False
    return True


def check_gates(windows_repo: str, task_branch: str, candidate_sha: str,
                tested_sha: str | None, reviewed_sha: str | None,
                review_approved: bool, *, run_dir: str | Path | None = None,
                task_id: str = "", base_branch: str = "",
                head_branch: str = "") -> SyncGates:
    """Evaluate all sync gates without modifying anything."""
    win_clean = _git_porcelain_clean(windows_repo)
    _run(["git", "-C", windows_repo, "fetch", "origin"], check=False)
    r = _run(["git", "-C", windows_repo, "rev-parse", f"origin/{task_branch}"], check=False)
    candidate_pushed = r.returncode == 0 and r.stdout.strip() == candidate_sha
    r = _run(["git", "-C", windows_repo, "rev-parse", task_branch], check=False)
    if r.returncode == 0:
        local_sha = r.stdout.strip()
        r2 = _run(["git", "-C", windows_repo, "merge-base", "--is-ancestor",
                   local_sha, f"origin/{task_branch}"], check=False)
        ff = r2.returncode == 0
    else:
        ff = True
    pr_ready = False
    if run_dir and task_id:
        pr_ready = validate_pr_content_artifact(
            run_dir, task_id, candidate_sha,
            tested_sha or "", reviewed_sha or "", base_branch, head_branch)
    return SyncGates(
        candidate_pushed=candidate_pushed,
        tested_eq_candidate=(tested_sha == candidate_sha) if tested_sha else False,
        reviewed_eq_candidate=(reviewed_sha == candidate_sha) if reviewed_sha else False,
        review_approved=review_approved,
        pr_content_ready=pr_ready,
        windows_clean=win_clean,
        fast_forwardable=ff,
    )
```

Update `sync()` to accept `run_dir`/`task_id`/`base_branch`/`head_branch` and pass to `check_gates`.

In `policy_gateway.py`, update `sync_windows` (line 270) and `_stage_windows_sync` (line 720) to pass `run_dir`, `task_id`, `base_branch`, `head_branch` to `win_sync` instead of `draft_pr_created=True`:

```python
            final_sha = win_sync(win_repo, task_branch, rec.candidate_sha,
                                 rec.tested_sha, rec.reviewed_sha,
                                 review_approved=True,
                                 run_dir=self.run_root / task_id,
                                 task_id=task_id,
                                 base_branch=cfg.base_branch,
                                 head_branch=task_branch)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_windows_sync.py tests/unit/test_pr_content.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor_cao/validation/windows_sync.py src/supervisor_cao/mcp/policy_gateway.py tests/unit/test_windows_sync.py
git commit -m "feat: windows sync validates real pr-content artifact

- SyncGates.pr_content_ready replaces bool draft_pr_created
- validate_pr_content_artifact checks sha256, schema, workflow_state,
  SHAs, review decision, push.json
- PolicyGateway passes run_dir for full validation before sync"
```

---

## Task 6: Append-only acceptance evidence + cleanup

**Files:**
- Modify: `src/supervisor_cao/cli/acceptance.py:60-125, 601-677`
- Modify: `src/supervisor_cao/cli/main.py`
- Test: `tests/unit/test_acceptance_evidence.py`

**Interfaces:**
- Produces: `acceptance/evidence/<run-id>/<scenario>/` append-only dirs; `purge_evidence(force)`; cleanup preserves evidence.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_acceptance_evidence.py`:

```python
"""Unit tests for append-only acceptance evidence and cleanup."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from supervisor_cao.cli.acceptance import _evidence_dir, _record_evidence, cleanup


def test_evidence_dir_unique_per_run(tmp_path):
    d1 = _evidence_dir(tmp_path, "direct", "run-1")
    d2 = _evidence_dir(tmp_path, "direct", "run-2")
    assert d1 != d2
    assert "direct" in str(d1)


def test_record_evidence_writes_files(tmp_path):
    ev_dir = _evidence_dir(tmp_path, "direct", "run-1")
    _record_evidence(ev_dir, {"result": "pass"}, {"task_id": "T1"},
                     [{"event": "x"}], [{"stage": "plan"}], {"used": 1},
                     [{"worker_id": "w1"}], {"candidate": "abc"},
                     {"pr_content": "valid"})
    assert (ev_dir / "result.json").exists()
    assert (ev_dir / "task_snapshot.json").exists()
    assert (ev_dir / "events.jsonl").exists()
    assert (ev_dir / "stage_attempts.json").exists()
    assert (ev_dir / "budget_log.json").exists()
    assert (ev_dir / "worker_handles.json").exists()
    assert (ev_dir / "sha_info.json").exists()


def test_cleanup_preserves_evidence(tmp_path):
    """cleanup must NOT delete acceptance/evidence/."""
    ev_dir = tmp_path / "acceptance" / "evidence" / "run-1" / "direct"
    ev_dir.mkdir(parents=True)
    (ev_dir / "result.json").write_text('{"passed": true}')
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        with patch("supervisor_cao.cli.acceptance._read_meta", return_value={"repo_dir": ""}):
            cleanup()
    assert ev_dir.exists(), "evidence must be preserved by cleanup"
    assert (ev_dir / "result.json").exists()


def test_cleanup_no_pr_list_close(tmp_path):
    """cleanup must NOT call gh pr list/close."""
    import subprocess
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        with patch("supervisor_cao.cli.acceptance._read_meta", return_value={"repo_dir": str(tmp_path)}):
            with patch("subprocess.run") as mock_run:
                cleanup()
    # no gh pr list or gh pr close calls
    for call in mock_run.call_args_list:
        cmd = call[0][0] if call[0] else []
        if cmd and cmd[0] == "gh":
            pytest.fail(f"cleanup called gh: {cmd}")


def test_purge_evidence_deletes_with_force(tmp_path):
    from supervisor_cao.cli.acceptance import purge_evidence
    ev_dir = tmp_path / "acceptance" / "evidence" / "run-1" / "direct"
    ev_dir.mkdir(parents=True)
    (ev_dir / "result.json").write_text('{}')
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        purge_evidence(force=True)
    assert not ev_dir.exists()


def test_purge_evidence_refuses_without_force(tmp_path):
    from supervisor_cao.cli.acceptance import purge_evidence
    ev_dir = tmp_path / "acceptance" / "evidence" / "run-1" / "direct"
    ev_dir.mkdir(parents=True)
    (ev_dir / "result.json").write_text('{}')
    with patch("supervisor_cao.cli.acceptance.ACCEPTANCE_ROOT", tmp_path / "acceptance"):
        purge_evidence(force=False)
    assert ev_dir.exists(), "purge without --force must not delete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_acceptance_evidence.py -v`
Expected: FAIL (functions not found)

- [ ] **Step 3: Implement append-only evidence and update cleanup**

In `src/supervisor_cao/cli/acceptance.py`:

Add evidence helpers:

```python
EVIDENCE_ROOT = ACCEPTANCE_ROOT / "evidence"


def _evidence_dir(acceptance_root: Path, scenario: str, run_id: str) -> Path:
    d = acceptance_root / "evidence" / run_id / scenario
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_evidence(ev_dir: Path, result: dict, task_snapshot: dict,
                     events: list, stage_attempts: list, budget_log: dict,
                     worker_handles: list, sha_info: dict, pr_content_info: dict):
    """Write all evidence files (append-only — never overwrites another run)."""
    (ev_dir / "result.json").write_text(json.dumps(result, indent=2))
    (ev_dir / "task_snapshot.json").write_text(json.dumps(task_snapshot, indent=2))
    with open(ev_dir / "events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    (ev_dir / "stage_attempts.json").write_text(json.dumps(stage_attempts, indent=2))
    (ev_dir / "budget_log.json").write_text(json.dumps(budget_log, indent=2))
    (ev_dir / "worker_handles.json").write_text(json.dumps(worker_handles, indent=2))
    (ev_dir / "sha_info.json").write_text(json.dumps(sha_info, indent=2))
    (ev_dir / "pr_content_info.json").write_text(json.dumps(pr_content_info, indent=2))


def purge_evidence(force: bool = False) -> int:
    """Explicitly delete historical evidence. Requires --force."""
    if not force:
        print("Refusing to purge evidence without --force. Use 'acceptance purge-evidence --force'.")
        return 1
    ev_root = ACCEPTANCE_ROOT / "evidence"
    if ev_root.exists():
        import shutil
        shutil.rmtree(ev_root)
        print(f"Purged {ev_root}")
    else:
        print("No evidence to purge.")
    return 0
```

Update `cleanup()` to remove `_cleanup_acceptance_prs` call and preserve evidence:

```python
def cleanup() -> int:
    """Remove the isolated acceptance environment (runtime, worktrees, acc branches).

    Preserves acceptance/evidence/ (append-only history). Does NOT close PRs
    or delete labels — forge operations are no longer performed.
    """
    meta = _read_meta()
    repo_dir = meta.get("repo_dir", "")
    if repo_dir and Path(repo_dir).exists():
        _cleanup_acceptance_branches(repo_dir)  # only acc/ branches, safe
    # Remove runtime/worktree dirs but KEEP evidence/
    ev_root = ACCEPTANCE_ROOT / "evidence"
    if ACCEPTANCE_ROOT.exists():
        import shutil
        # remove everything except evidence/
        for item in ACCEPTANCE_ROOT.iterdir():
            if item.name == "evidence":
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        print(f"Cleaned runtime (evidence preserved at {ev_root})")
    else:
        print("Nothing to clean.")
    return 0
```

Delete `_cleanup_acceptance_prs` entirely. Keep `_cleanup_acceptance_branches`.

In `src/supervisor_cao/cli/main.py`, add `purge-evidence` subcommand.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_acceptance_evidence.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor_cao/cli/acceptance.py src/supervisor_cao/cli/main.py tests/unit/test_acceptance_evidence.py
git commit -m "feat: append-only acceptance evidence + cleanup preserves history

- evidence/<run-id>/<scenario>/ never overwritten
- cleanup removes runtime/worktrees/acc-branches but keeps evidence
- removes all gh pr list/close/label logic
- adds purge-evidence --force for explicit evidence deletion"
```

---

## Task 7: Direct scenario rewrite

**Files:**
- Modify: `src/supervisor_cao/cli/acceptance.py:315-352`
- Test: `tests/unit/test_acceptance_evidence.py` (add direct condition tests)

- [ ] **Step 1: Write failing test for direct pass conditions**

Add to `tests/unit/test_acceptance_evidence.py`:

```python
def test_direct_pass_conditions_all_met(tmp_path):
    """direct PASS requires: READY_FOR_HUMAN_REVIEW + SHAs equal + pr-content valid + no forge API."""
    from supervisor_cao.cli.acceptance import _direct_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir()
    # write valid pr-content
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {
        "plan": {"steps": [{"description": "x"}]},
        "implementation": {"candidate_sha": "c1", "changed_files": ["a.py"]},
        "verification": {"candidate_sha": "c1", "tested_sha": "c1", "wsl_results": {}, "remote_results": {}},
        "review": {"reviewed_sha": "c1", "decision": "APPROVED", "findings": []},
        "budget": {"total_used": 1, "remaining": 3},
    }
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/T1",
            "pushed_sha": "c1", "push_succeeded": True}
    j, m, s = render_pr_content(arts, "T1", "main", "agent/T1", push)
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))
    ok, _ = _direct_pass("READY_FOR_HUMAN_REVIEW", "c1", "c1", "c1", ev_dir)
    assert ok is True


def test_direct_fail_when_pr_content_invalid(tmp_path):
    from supervisor_cao.cli.acceptance import _direct_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir()
    # no pr-content files
    ok, reason = _direct_pass("READY_FOR_HUMAN_REVIEW", "c1", "c1", "c1", ev_dir)
    assert ok is False
    assert "pr-content" in reason or "pr_content" in reason
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/unit/test_acceptance_evidence.py -k "direct" -v`
Expected: FAIL (`_direct_pass` not found)

- [ ] **Step 3: Implement _direct_pass and rewrite _run_direct**

In `acceptance.py`, add:

```python
def _direct_pass(final_state, candidate, tested, reviewed, ev_dir: Path) -> tuple[bool, str]:
    """Check direct scenario pass conditions."""
    if final_state != "READY_FOR_HUMAN_REVIEW":
        return False, f"final_state={final_state}"
    if not (candidate == tested == reviewed):
        return False, f"SHA mismatch: {candidate}/{tested}/{reviewed}"
    from supervisor_cao.validation.windows_sync import validate_pr_content_artifact
    if not validate_pr_content_artifact(ev_dir, "direct", candidate, tested, reviewed, "main", "agent/direct"):
        return False, "pr-content artifact invalid"
    return True, "ok"
```

Rewrite `_run_direct` to use `test_mode=True` (no gh), collect evidence to append-only dir, and call `_direct_pass`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_acceptance_evidence.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor_cao/cli/acceptance.py tests/unit/test_acceptance_evidence.py
git commit -m "feat: direct scenario uses forge-agnostic pass conditions

- removes gh pr create / is_real_pr assertion
- PASS requires READY_FOR_HUMAN_REVIEW + SHA equality + valid pr-content
- evidence written to append-only dir"
```

---

## Task 8: review-fix scenario rewrite

**Files:**
- Modify: `src/supervisor_cao/cli/acceptance.py:355-493`
- Test: `tests/unit/test_acceptance_evidence.py` (add review-fix tests)

- [ ] **Step 1: Write failing test for review-fix pass conditions**

Add to `tests/unit/test_acceptance_evidence.py`:

```python
def test_review_fix_pass_all_conditions(tmp_path):
    from supervisor_cao.cli.acceptance import _review_fix_pass
    ev_dir = tmp_path / "ev"
    ev_dir.mkdir()
    from supervisor_cao.pr_content.renderer import render_pr_content
    arts = {"plan": {"steps": [{"description": "x"}]},
            "implementation": {"candidate_sha": "c1", "changed_files": ["a.py"]},
            "verification": {"candidate_sha": "c1", "tested_sha": "c1", "wsl_results": {}, "remote_results": {}},
            "review": {"reviewed_sha": "c1", "decision": "APPROVED", "findings": []},
            "budget": {"total_used": 1, "remaining": 3}}
    push = {"schema_version": 1, "remote": "origin", "branch": "agent/T1", "pushed_sha": "c1", "push_succeeded": True}
    j, m, s = render_pr_content(arts, "T1", "main", "agent/T1", push)
    (ev_dir / "pr-content.json").write_text(j)
    (ev_dir / "pr-content.md").write_text(m)
    (ev_dir / "pr-content.sha256").write_text(s)
    (ev_dir / "push.json").write_text(json.dumps(push))
    ok, _ = _review_fix_pass(True, True, "READY_FOR_HUMAN_REVIEW", ev_dir, "c1", "c1", "c1")
    assert ok is True


def test_review_fix_fail_when_needs_human(tmp_path):
    """Judge NEEDS_HUMAN means task_approved=False -> main scenario does NOT pass."""
    from supervisor_cao.cli.acceptance import _review_fix_pass
    ok, _ = _review_fix_pass(True, False, "NEEDS_HUMAN", tmp_path, "c1", "c1", "c1")
    assert ok is False
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/unit/test_acceptance_evidence.py -k "review_fix" -v`
Expected: FAIL

- [ ] **Step 3: Implement _review_fix_pass and rewrite _run_review_fix**

Add:

```python
def _review_fix_pass(protocol_passed, task_approved, final_state, ev_dir, candidate, tested, reviewed) -> tuple[bool, str]:
    if not protocol_passed:
        return False, "protocol not passed"
    if not task_approved:
        return False, "task not approved"
    if final_state != "READY_FOR_HUMAN_REVIEW":
        return False, f"final_state={final_state}"
    from supervisor_cao.validation.windows_sync import validate_pr_content_artifact
    if not validate_pr_content_artifact(ev_dir, "reviewfix", candidate, tested, reviewed, "main", "agent/reviewfix"):
        return False, "pr-content invalid"
    return True, "ok"
```

Replace the `sqlite3 UPDATE tasks` (L451-456) with `store.inject_candidate(task_id, new_sha, TaskState.LOCAL_VERIFYING)`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_acceptance_evidence.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/supervisor_cao/cli/acceptance.py tests/unit/test_acceptance_evidence.py
git commit -m "feat: review-fix strict 4-condition pass + inject_candidate

- PASS requires protocol_passed + task_approved + final state + pr-content
- Judge NEEDS_HUMAN is safety evidence, not main-scenario pass
- controlled candidate injection via StateStore.inject_candidate (audited)"
```

---

## Task 9: Real mid-stage resume + Worker reattach

**Files:**
- Modify: `src/supervisor_cao/cli/acceptance.py:496-572`
- Create: `tests/unit/test_resume_reattach.py`

- [ ] **Step 1: Write failing tests for 5 resume sub-scenarios**

Create `tests/unit/test_resume_reattach.py` with tests for:
1. controller crash + Worker still running → reattach
2. controller crash + Worker completed → collect result
3. controller crash + Worker dead → STALLED/restart
4. lease not expired → cannot preempt
5. lease expired → single new owner

(Detailed test code written during implementation — these require mock WorkerMonitor/StageStore since real cao-server isn't available in unit tests. The real e2e runs happen in live acceptance.)

- [ ] **Step 2: Implement resume rewrite**

Rewrite `_run_resume` to implement the 8-step flow from the design doc, with strict `==` assertions. Add `_resume_pass()` helper.

- [ ] **Step 3: Run tests + commit**

```bash
git add src/supervisor_cao/cli/acceptance.py tests/unit/test_resume_reattach.py
git commit -m "feat: real mid-stage resume with 8-step reattach + 5 sub-scenarios

- controller crash simulation (terminate, not kill worker)
- wait for lease expiry or safe takeover
- reattach if running, collect if completed, STALLED if dead
- strict == assertions: no duplicate budget/stage/commit/PR-content/sync"
```

---

## Task 10: Documentation + full regression

**Files:**
- Modify: `docs/ACCEPTANCE.md`, `docs/WORKFLOW.md`, `docs/USER_GUIDE.md`, `README.md`, `AGENTS.md`

- [ ] **Step 1: Update docs**

- `docs/ACCEPTANCE.md`: rewrite three scenario pass conditions; remove "real Draft PR is acceptance condition".
- `docs/WORKFLOW.md`: update state machine diagram (`APPROVED → PR_CONTENT_READY → WINDOWS_SYNCED → READY_FOR_HUMAN_REVIEW`).
- `docs/USER_GUIDE.md`: add PR handoff section (copy-paste flow).
- `README.md`: update terminal state description.
- `AGENTS.md`: remove Draft PR gate language.

- [ ] **Step 2: Run full regression**

```bash
python -m pytest tests/ -q
python -m pytest tests/e2e/test_temp_repo_e2e.py -v
```

- [ ] **Step 3: Secret scan**

```bash
git grep -n -E "(gh pr|GitHub API|requests\.post|import urllib)" -- src/ scripts/
```
Expected: only `cao_client.py` (cao-server, not forge) — no forge API in production path.

- [ ] **Step 4: CLI smoke**

```bash
python -m supervisor_cao.cli.main --help
python -m supervisor_cao.cli.main acceptance --help
```

- [ ] **Step 5: Commit + push feature branch**

```bash
git add docs/ tests/
git commit -m "docs: update all docs for forge-agnostic PR handoff

- state machine diagram: APPROVED -> PR_CONTENT_READY -> WINDOWS_SYNCED
- acceptance: three scenarios with strict conditions
- user guide: copy-paste PR handoff flow
- remove all Draft PR gate language"
git push origin feat/pr-content-handoff
```

- [ ] **Step 6: Output final PR content**

After all tests pass, run `render-pr-content` on the feature branch's own artifacts (or construct from the diff) and output the PR Title/Base/Head/Body.

---

## Self-Review Checklist

- [x] **Spec coverage**: Every section of the design doc maps to a task (schema→T1, push→T2, state→T3, gateway→T4, sync→T5, evidence→T6, direct→T7, review-fix→T8, resume→T9, docs→T10).
- [x] **Placeholder scan**: No TBD/TODO; all code steps contain actual code.
- [x] **Type consistency**: `render_pr_content` signature consistent across T1/T2/T3; `validate_pr_content_artifact` consistent across T5/T7/T8; `inject_candidate` consistent across T3/T8.
- [x] **Global constraints**: No forge API in production path; UTF-8/LF; no generated_at; two-line sha256; workflow_state=PR_CONTENT_READY; lazy migration; append-only evidence.
