"""Unit tests for worker_runner: strict JSON extraction + schema validation.

Requirement 4: no greedy {.*} regex. Tests cover fenced JSON, balanced-bracket
extraction, multi-object rejection, trailing-content rejection, and schema
validation pass/fail.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.mcp.worker_runner import (  # noqa: E402
    extract_strict_json, validate_and_stamp, WorkerError,
)


class TestStrictJsonExtraction:
    def test_plain_json(self):
        obj = extract_strict_json('{"a": 1, "b": "x"}')
        assert obj == {"a": 1, "b": "x"}

    def test_fenced_json(self):
        text = 'Here is my result:\n```json\n{"plan_id": "P1", "task_id": "T1"}\n```'
        obj = extract_strict_json(text)
        assert obj["plan_id"] == "P1"

    def test_fenced_plain(self):
        text = '```\n{"x": 2}\n```'
        assert extract_strict_json(text) == {"x": 2}

    def test_json_with_nested_braces_in_strings(self):
        # braces inside string literals must not affect depth
        text = '{"msg": "function() { return {a: 1}; }", "n": 3}'
        obj = extract_strict_json(text)
        assert obj["n"] == 3
        assert "{a: 1}" in obj["msg"]

    def test_json_with_escaped_quotes(self):
        text = '{"msg": "he said \\"hi\\""}'
        obj = extract_strict_json(text)
        assert obj["msg"] == 'he said "hi"'

    def test_rejects_empty(self):
        with pytest.raises(WorkerError, match="empty"):
            extract_strict_json("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(WorkerError, match="empty"):
            extract_strict_json("   \n  ")

    def test_rejects_no_json(self):
        with pytest.raises(WorkerError, match="no JSON object"):
            extract_strict_json("I could not produce JSON, sorry.")

    def test_rejects_multiple_objects(self):
        text = '{"a": 1}\n{"b": 2}'
        with pytest.raises(WorkerError, match="multiple JSON objects"):
            extract_strict_json(text)

    def test_rejects_trailing_content(self):
        text = '{"a": 1}\nThis is extra non-JSON content.'
        with pytest.raises(WorkerError, match="trailing content"):
            extract_strict_json(text)

    def test_allows_leading_preamble(self):
        # Workers often emit a header before the JSON; that's fine as long as
        # there is exactly one object and no trailing content.
        text = '## Executor result\n\n{"candidate_sha": "abc123"}'
        obj = extract_strict_json(text)
        assert obj["candidate_sha"] == "abc123"

    def test_fenced_with_trailing_fence_only(self):
        # closing fence after the object inside a fenced block is OK
        text = '```json\n{"x": 1}\n```'
        assert extract_strict_json(text) == {"x": 1}


class TestValidateAndStamp:
    def test_stamps_cross_artifact_fields(self):
        obj = {
            "plan_id": "P1", "task_id": "T1", "target_files": ["a.py"],
            "steps": [{"description": "do X"}], "test_matrix": ["test_a"],
            "rollback_conditions": ["revert"], "completion_criteria": ["done"],
            "prerequisites_verified": True, "baseline_sha": "abc", "model": "codex",
        }
        out = validate_and_stamp("plan", obj, "T1", candidate_sha="abc")
        assert out["task_id"] == "T1"
        assert out["stage"] == "plan"
        assert out["schema_version"] == "1"
        assert out["candidate_sha"] == "abc"

    def test_invalid_schema_rejected(self):
        obj = {"plan_id": "P1"}  # missing many required fields
        with pytest.raises(Exception):
            validate_and_stamp("plan", obj, "T1")

    def test_unknown_stage_rejected(self):
        with pytest.raises(WorkerError, match="unknown stage"):
            validate_and_stamp("bogus", {}, "T1")
