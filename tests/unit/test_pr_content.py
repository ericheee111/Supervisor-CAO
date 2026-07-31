"""Unit tests for PR content renderer (forge-agnostic, no network)."""
import hashlib
import inspect
import json
import subprocess
import sys
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
    j = json.loads(json_text)
    assert j["task_id"] == "T1"
    assert j["workflow_state"] == "PR_CONTENT_READY"
    assert j["candidate_sha"] == "abc123"
    assert j["title"] == "T1"


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


def test_renderer_source_has_no_network_imports():
    """The renderer module must not import requests/urllib or call gh."""
    import supervisor_cao.pr_content.renderer as mod
    src = inspect.getsource(mod)
    assert "import requests" not in src
    assert "import urllib" not in src
    assert "subprocess" not in src


# --- CLI script tests ---

def _write_artifacts(run_dir: Path, arts: dict, push: dict | None = None,
                     task_id: str = "T1", head_branch: str = "agent/T1"):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "plan.json").write_text(json.dumps(arts["plan"]))
    (run_dir / "implementation.json").write_text(json.dumps(arts["implementation"]))
    (run_dir / "verification.json").write_text(json.dumps(arts["verification"]))
    (run_dir / "review.json").write_text(json.dumps(arts["review"]))
    (run_dir / "codex-budget-summary.json").write_text(json.dumps(arts["budget"]))
    if push:
        # ensure push branch matches the head_branch used in the test
        p = dict(push)
        p["branch"] = head_branch
        (run_dir / "push.json").write_text(json.dumps(p))


def _script_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "render-pr-content"


def test_cli_produces_files_and_stdout(tmp_path):
    run_dir = tmp_path / "T1"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push(),
                     task_id="T1", head_branch="agent/T1")
    r = subprocess.run(
        [sys.executable, str(_script_path()), "--task-id", "T1",
         "--base-branch", "main", "--head-branch", "agent/T1",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (run_dir / "pr-content.json").exists()
    assert (run_dir / "pr-content.md").exists()
    assert (run_dir / "pr-content.sha256").exists()
    assert "PR Title:" in r.stdout
    assert "Base:" in r.stdout
    assert "Head:" in r.stdout
    assert "PR Body:" in r.stdout


def test_cli_rejects_missing_push_json(tmp_path):
    run_dir = tmp_path / "T2"
    _write_artifacts(run_dir, _sample_artifacts())  # no push.json
    r = subprocess.run(
        [sys.executable, str(_script_path()), "--task-id", "T2",
         "--base-branch", "main", "--head-branch", "agent/T2",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "push" in r.stderr.lower()


def test_cli_idempotent_rerun(tmp_path):
    run_dir = tmp_path / "T3"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push(),
                     task_id="T3", head_branch="agent/T3")
    cmd = [sys.executable, str(_script_path()), "--task-id", "T3",
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


def test_cli_does_not_force_draft_title(tmp_path):
    run_dir = tmp_path / "T5"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push(),
                     task_id="T5", head_branch="agent/T5")
    r = subprocess.run(
        [sys.executable, str(_script_path()), "--task-id", "T5",
         "--base-branch", "main", "--head-branch", "agent/T5",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    j = json.loads((run_dir / "pr-content.json").read_text())
    assert not j["title"].startswith("[Draft]")


def test_create_draft_pr_wrapper_is_deprecated(tmp_path):
    """create-draft-pr wrapper should print deprecation and call render-pr-content."""
    run_dir = tmp_path / "T6"
    _write_artifacts(run_dir, _sample_artifacts(), _sample_push(),
                     task_id="T6", head_branch="agent/T6")
    wrapper = Path(__file__).resolve().parents[2] / "scripts" / "create-draft-pr"
    r = subprocess.run(
        [sys.executable, str(wrapper), "--task-id", "T6",
         "--base-branch", "main", "--head-branch", "agent/T6",
         "--run-dir", str(run_dir)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "DEPRECATED" in r.stderr
    # should have produced pr-content files, not draft-pr-url.txt
    assert (run_dir / "pr-content.json").exists()
    assert not (run_dir / "draft-pr-url.txt").exists()
