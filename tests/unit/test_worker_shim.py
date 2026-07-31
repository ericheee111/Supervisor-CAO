"""Unit tests for worker-shim persistent launcher."""
import json
import subprocess
import sys
from pathlib import Path

import pytest


def _shim_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "worker-shim"


def test_shim_runs_command_and_writes_files(tmp_path):
    """Shim should run a simple command and write exit-code + result.json."""
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    result = tmp_path / "result.json"
    exit_code = tmp_path / "exit.txt"
    r = subprocess.run(
        [sys.executable, str(_shim_path()),
         "--stdout", str(stdout), "--stderr", str(stderr),
         "--result", str(result), "--exit-code", str(exit_code),
         "--", sys.executable, "-c", "print('hello world')"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert exit_code.exists()
    assert exit_code.read_text().strip() == "0"
    assert result.exists()
    data = json.loads(result.read_text())
    assert data["done"] is True
    assert data["success"] is True
    assert data["exit_code"] == 0
    assert "hello world" in stdout.read_text()


def test_shim_writes_nonzero_exit_code(tmp_path):
    """Shim should write the correct non-zero exit code."""
    exit_code = tmp_path / "exit.txt"
    r = subprocess.run(
        [sys.executable, str(_shim_path()),
         "--stdout", str(tmp_path / "out.log"), "--stderr", str(tmp_path / "err.log"),
         "--result", str(tmp_path / "result.json"), "--exit-code", str(exit_code),
         "--", sys.executable, "-c", "import sys; sys.exit(42)"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 42
    assert exit_code.read_text().strip() == "42"
    data = json.loads((tmp_path / "result.json").read_text())
    assert data["exit_code"] == 42
    assert data["success"] is False


def test_shim_writes_partial_result_on_start(tmp_path):
    """Shim should write a partial result file immediately on start."""
    result = tmp_path / "result.json"
    # Use a command that takes a moment so we can observe the partial file
    r = subprocess.run(
        [sys.executable, str(_shim_path()),
         "--stdout", str(tmp_path / "out.log"), "--stderr", str(tmp_path / "err.log"),
         "--result", str(result), "--exit-code", str(tmp_path / "exit.txt"),
         "--", sys.executable, "-c", "import time; time.sleep(0.5); print('done')"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    # Final result should be complete
    data = json.loads(result.read_text())
    assert data["done"] is True


def test_shim_extracts_last_message_from_ndjson(tmp_path):
    """Shim should extract last_message from OpenCode-style NDJSON."""
    ndjson = '{"type":"assistant","content":[{"type":"text","text":"final answer"}]}'
    r = subprocess.run(
        [sys.executable, str(_shim_path()),
         "--stdout", str(tmp_path / "out.log"), "--stderr", str(tmp_path / "err.log"),
         "--result", str(tmp_path / "result.json"), "--exit-code", str(tmp_path / "exit.txt"),
         "--", sys.executable, "-c", f"print('''{ndjson}''')"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    data = json.loads((tmp_path / "result.json").read_text())
    assert data.get("last_message") == "final answer"


def test_shim_handles_command_error(tmp_path):
    """Shim should handle a missing command gracefully."""
    r = subprocess.run(
        [sys.executable, str(_shim_path()),
         "--stdout", str(tmp_path / "out.log"), "--stderr", str(tmp_path / "err.log"),
         "--result", str(tmp_path / "result.json"), "--exit-code", str(tmp_path / "exit.txt"),
         "--", "/nonexistent/command"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert (tmp_path / "exit.txt").exists()


def test_shim_creates_parent_dirs(tmp_path):
    """Shim should create parent directories for log files."""
    stdout = tmp_path / "nested" / "dir" / "out.log"
    r = subprocess.run(
        [sys.executable, str(_shim_path()),
         "--stdout", str(stdout), "--stderr", str(tmp_path / "err.log"),
         "--result", str(tmp_path / "result.json"), "--exit-code", str(tmp_path / "exit.txt"),
         "--", sys.executable, "-c", "print('ok')"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert stdout.exists()
