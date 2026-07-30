"""Unit tests for JSON schema validation (spec §9, §20.1)."""
import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import validate, ValidationError

SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _load(name):
    return json.loads((SCHEMAS_DIR / name).read_text())


def test_task_schema_valid():
    schema = _load("task.schema.json")
    validate({
        "task_id": "T1", "project": "demo-project", "description": "optimize X",
        "base_branch": "main",
    }, schema)


def test_task_schema_missing_required():
    schema = _load("task.schema.json")
    with pytest.raises(ValidationError):
        validate({"project": "demo-project"}, schema)


def test_plan_schema_valid():
    schema = _load("plan.schema.json")
    validate({
        "plan_id": "P1", "task_id": "T1", "target_files": ["a.py"],
        "steps": [{"description": "do X", "file": "a.py", "risk_level": "low"}],
        "test_matrix": ["test_a"], "rollback_conditions": ["revert"],
        "completion_criteria": ["faster"], "prerequisites_verified": True,
        "baseline_sha": "abc", "model": "codex",
    }, schema)


def test_review_schema_approved():
    schema = _load("review.schema.json")
    validate({
        "review_id": "R1", "task_id": "T1", "candidate_sha": "c1",
        "reviewed_sha": "c1", "decision": "APPROVED", "findings": [], "summary": "ok",
        "model": "codex",
    }, schema)


def test_review_schema_finding_severity_enum():
    schema = _load("review.schema.json")
    with pytest.raises(ValidationError):
        validate({
            "review_id": "R1", "task_id": "T1", "candidate_sha": "c1",
            "reviewed_sha": "c1", "decision": "CHANGES_REQUESTED",
            "findings": [{"id": "F1", "severity": "P9", "category": "bug",
                          "file": "a.py", "claim": "bad", "evidence": "e",
                          "recommended_direction": "fix"}],
            "summary": "issues", "model": "codex",
        }, schema)


def test_decision_schema_valid():
    schema = _load("decision.schema.json")
    validate({
        "decision_id": "D1", "task_id": "T1", "dispute_id": "S1",
        "candidate_sha": "c1", "ruling": "uphold_finding", "rationale": "because",
        "evidence_cited": ["e1"], "new_evidence_present": False, "model": "codex",
    }, schema)


def test_verification_schema_valid():
    schema = _load("verification.schema.json")
    validate({
        "task_id": "T1", "candidate_sha": "c1", "tested_sha": "c1", "passed": True,
        "wsl_results": {"build": True, "pytest_passed": True, "summary": "ok"},
        "remote_results": {"container": "C1", "install_ok": True,
                           "correctness_passed": True, "summary": "ok"},
        "environment": {}, "logs": {},
    }, schema)


def test_implementation_schema_valid():
    schema = _load("implementation.schema.json")
    validate({
        "task_id": "T1", "candidate_sha": "c1", "base_sha": "b1",
        "changed_files": ["a.py"], "commit_message": "fix", "rounds": 1,
        "self_check_passed": True,
        "focused_tests": {"run": True, "passed": True, "summary": "ok"},
    }, schema)
