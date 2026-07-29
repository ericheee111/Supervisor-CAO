"""Unit tests for the secret scanner (spec §4, §20.1)."""
import importlib.util
import sys
from pathlib import Path

import pytest

# load scan-secrets as a module (it has no .py extension)
from importlib.machinery import SourceFileLoader
scan_mod = SourceFileLoader(
    "scan_secrets", str(Path(__file__).resolve().parents[2] / "scripts" / "scan-secrets")
).load_module()


def test_clean_file_no_findings(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("this is a normal file with no secrets")
    assert scan_mod.scan_file(f) == []


def test_openai_key_detected(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz1234567890")
    findings = scan_mod.scan_file(f)
    assert any("sk-" in s for s in findings)


def test_github_token_detected(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text("token = ghp_0123456789abcdefghijklmnopqrstuvwxyz0123")
    findings = scan_mod.scan_file(f)
    assert any("GitHub token" in s for s in findings)


def test_private_identifier_kpserver(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text("host: kpserver")
    findings = scan_mod.scan_file(f)
    assert any("kpserver" in s for s in findings)


def test_private_identifier_container(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text("container: ericheee-cpu200-203")
    findings = scan_mod.scan_file(f)
    assert any("ericheee-cpu200-203" in s for s in findings)


def test_private_path_leak(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("config at /root/.config/supervisor-cao")
    findings = scan_mod.scan_file(f)
    assert any("private path" in s for s in findings)


def test_forbidden_file_detected(tmp_path):
    (tmp_path / "secrets.env").write_text("KEY=val")
    findings, forbidden = scan_mod.scan_repo(str(tmp_path))
    assert any("secrets.env" in f for f in forbidden)


def test_local_yaml_forbidden(tmp_path):
    (tmp_path / "pandas.local.yaml").write_text("name: pandas")
    findings, forbidden = scan_mod.scan_repo(str(tmp_path))
    assert any("local.yaml" in f for f in forbidden)


def test_private_md_forbidden(tmp_path):
    (tmp_path / "secret.private.md").write_text("secret design")
    findings, forbidden = scan_mod.scan_repo(str(tmp_path))
    assert any("private.md" in f for f in forbidden)


def test_clean_repo_passes(tmp_path):
    (tmp_path / "main.py").write_text("print('hello world')")
    findings, forbidden = scan_mod.scan_repo(str(tmp_path))
    assert findings == []
    assert forbidden == []
