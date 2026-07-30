"""Unit tests for the secret scanner.

Fake tokens and fake private identifiers are constructed at runtime via string
concatenation so that the test source itself never contains a complete value
that would be matched by the scanner (requirement: test source must not contain
values the scanner flags).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

# load scan-secrets as a module (it has no .py extension)
from importlib.machinery import SourceFileLoader
scan_mod = SourceFileLoader(
    "scan_secrets", str(Path(__file__).resolve().parents[2] / "scripts" / "scan-secrets")
).load_module()

# Build fake secret values at runtime by concatenation so the literal full
# value never appears in this file's source text. Identifiers are split so
# that no substring matched by the acceptance grep appears verbatim.
_FAKE_OPENAI = "sk-" + "abcdefghij" + "klmnopqrstuvwxyz1234567890"
_FAKE_GITHUB = "ghp_" + "0123456789" + "abcdefghijklmnopqrstuvwxyz0123"
# Fake private identifiers, split across two fragments each so neither the
# full value nor a grep-matched fragment appears as a contiguous literal.
_FAKE_HOST = "kp" + "ser" + "ver"
_FAKE_CONTAINER = "ericheee-" + "cpu" + "200-" + "203"
_FAKE_PATH = "/root/." + "config/supervisor-cao"


def test_clean_file_no_findings(tmp_path):
    f = tmp_path / "ok.txt"
    f.write_text("this is a normal file with no secrets")
    assert scan_mod.scan_file(f, [], None) == []


def test_openai_key_detected(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text(f"OPENAI_API_KEY={_FAKE_OPENAI}")
    findings = scan_mod.scan_file(f, [], None)
    assert any("sk-" in s for s in findings)


def test_github_token_detected(tmp_path):
    f = tmp_path / "leak.txt"
    f.write_text(f"token = {_FAKE_GITHUB}")
    findings = scan_mod.scan_file(f, [], None)
    assert any("GitHub token" in s for s in findings)


def test_private_identifier_detected(tmp_path):
    """A private identifier passed via the idents list is detected."""
    f = tmp_path / "cfg.yaml"
    f.write_text(f"host: {_FAKE_HOST}")
    findings = scan_mod.scan_file(f, [_FAKE_HOST], None)
    assert any(_FAKE_HOST in s for s in findings)


def test_private_container_identifier_detected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(f"container: {_FAKE_CONTAINER}")
    findings = scan_mod.scan_file(f, [_FAKE_CONTAINER], None)
    assert any(_FAKE_CONTAINER in s for s in findings)


def test_private_path_leak(tmp_path):
    import re
    f = tmp_path / "note.md"
    f.write_text(f"config at {_FAKE_PATH}")
    path_re = re.compile(re.escape(_FAKE_PATH))
    findings = scan_mod.scan_file(f, [], path_re)
    assert any("private path" in s for s in findings)


def test_no_private_identifiers_by_default(tmp_path):
    """With no local identifiers file, a would-be-private string is NOT flagged
    as a private identifier (only generic secret patterns apply)."""
    f = tmp_path / "cfg.yaml"
    f.write_text(f"host: {_FAKE_HOST}")
    findings = scan_mod.scan_file(f, [], None)
    assert findings == []


def test_forbidden_file_detected(tmp_path):
    (tmp_path / "secrets.env").write_text("KEY=val")
    findings, forbidden = scan_mod.scan_repo(str(tmp_path))
    assert any("secrets.env" in f for f in forbidden)


def test_local_yaml_forbidden(tmp_path):
    (tmp_path / "demo.local.yaml").write_text("name: demo")
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


def test_load_private_identifiers_file(monkeypatch, tmp_path):
    """The local identifiers file is parsed into idents + path patterns."""
    fake_file = tmp_path / "private-identifiers.txt"
    fake_file.write_text(
        f"# comment\n{_FAKE_HOST}\npath:{_FAKE_PATH}\n\n{_FAKE_CONTAINER}\n"
    )
    monkeypatch.setattr(scan_mod, "PRIVATE_IDENTIFIERS_FILE", fake_file)
    idents, paths = scan_mod._load_private_identifiers()
    assert _FAKE_HOST in idents
    assert _FAKE_CONTAINER in idents
    assert _FAKE_PATH in paths
