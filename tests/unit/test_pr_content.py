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
