"""Unit tests for model_resolver (spec §19: model ids from local config)."""
from pathlib import Path

import pytest


def test_resolve_model_returns_none_when_no_file(monkeypatch, tmp_path):
    import supervisor_cao.projects.model_resolver as mr
    monkeypatch.setattr(mr, "MODELS_LOCAL_FILE", tmp_path / "missing.yaml")
    assert mr.resolve_model("glm-executor") is None


def test_resolve_model_from_flat_mapping(monkeypatch, tmp_path):
    import supervisor_cao.projects.model_resolver as mr
    f = tmp_path / "models.local.yaml"
    f.write_text("executor: some-provider/some-model\n")
    monkeypatch.setattr(mr, "MODELS_LOCAL_FILE", f)
    assert mr.resolve_model("glm-executor") == "some-provider/some-model"


def test_resolve_model_from_nested_roles(monkeypatch, tmp_path):
    import supervisor_cao.projects.model_resolver as mr
    f = tmp_path / "models.local.yaml"
    f.write_text("roles:\n  verifier: p2/m2\n")
    monkeypatch.setattr(mr, "MODELS_LOCAL_FILE", f)
    assert mr.resolve_model("qwen-verifier") == "p2/m2"


def test_resolve_model_unknown_profile_returns_none(monkeypatch, tmp_path):
    import supervisor_cao.projects.model_resolver as mr
    monkeypatch.setattr(mr, "MODELS_LOCAL_FILE", tmp_path / "missing.yaml")
    assert mr.resolve_model("unknown-profile") is None


def test_resolve_model_malformed_file_returns_none(monkeypatch, tmp_path):
    import supervisor_cao.projects.model_resolver as mr
    f = tmp_path / "models.local.yaml"
    f.write_text(":::not yaml:::\n")
    monkeypatch.setattr(mr, "MODELS_LOCAL_FILE", f)
    assert mr.resolve_model("glm-executor") is None
