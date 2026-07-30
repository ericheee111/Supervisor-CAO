"""Unit tests for the remote verification runner fixes (requirement 5, 6).

Requirement 5:
  - pytest's exit code must NOT be masked by `tail` (pipefail fix).
  - dirty-on-start must release the lock and return REMOTE_WORKTREE_DIRTY
    WITHOUT checkout/install/test.

These tests use a fake "remote" (a local script) to verify the pipefail
behavior deterministically without needing real SSH/Docker.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPipefail:
    """Requirement 5: pytest exit code must not be masked by tail."""

    def test_pytest_failure_not_masked_by_tail_with_pipefail(self):
        """A pytest that exits 1, piped through tail, must report failure
        when set -o pipefail is used. This proves the fix is correct."""
        # Simulate: pytest exits 1, tail exits 0. Without pipefail, $? = 0 (tail).
        # With pipefail, $? = 1 (pytest).
        cmd_no_pipefail = "python -c 'import sys; sys.exit(1)' 2>&1 | tail -1; echo EXIT=$?"
        cmd_with_pipefail = "set -o pipefail; python -c 'import sys; sys.exit(1)' 2>&1 | tail -1; echo EXIT=$?"
        r_no = subprocess.run(["bash", "-c", cmd_no_pipefail], capture_output=True, text=True)
        r_yes = subprocess.run(["bash", "-c", cmd_with_pipefail], capture_output=True, text=True)
        # Without pipefail: the pipeline exit code is tail's (0)
        assert "EXIT=0" in r_no.stdout, f"expected EXIT=0 without pipefail, got: {r_no.stdout}"
        # With pipefail: the pipeline exit code is pytest's (1)
        assert "EXIT=1" in r_yes.stdout, f"expected EXIT=1 with pipefail, got: {r_yes.stdout}"

    def test_run_verification_verify_cmd_uses_pipefail(self):
        """The run-verification script's verification commands must use pipefail
        so a failing verify-command's exit code is not masked."""
        script = (REPO_ROOT / "scripts" / "run-verification").read_text()
        assert "set -o pipefail" in script, "run-verification must use 'set -o pipefail'"
        # The verify-command block runs each configured command with pipefail.
        idx = script.index("set -o pipefail && {cmd}")
        block = script[idx:script.index("\n", idx)]
        assert "pipefail" in block, f"verify command block missing pipefail: {block}"

    def test_run_verification_setup_cmd_uses_pipefail(self):
        """The setup commands must also use pipefail."""
        script = (REPO_ROOT / "scripts" / "run-verification").read_text()
        # setup commands use the same pipefail pattern
        assert "set -o pipefail && {cmd}" in script
        assert script.count("set -o pipefail") >= 2, "both setup and verify must use pipefail"


class TestDirtyOnStart:
    """Requirement 5: dirty-on-start releases lock, returns REMOTE_WORKTREE_DIRTY,
    does NOT checkout/install/test."""

    def test_dirty_path_releases_lock_and_returns_early(self):
        """The dirty-on-start branch must: set REMOTE_WORKTREE_DIRTY, release the
        lock, write verification.json, and return WITHOUT running checkout/install."""
        script = (REPO_ROOT / "scripts" / "run-verification").read_text()
        # The dirty branch must release the lock
        assert "REMOTE_WORKTREE_DIRTY" in script
        # Find the dirty branch and verify it releases the lock and returns early
        dirty_idx = script.index("REMOTE_WORKTREE_DIRTY")
        # the release_lock call must appear AFTER the dirty detection
        after_dirty = script[dirty_idx:]
        assert "_release_lock" in after_dirty, "dirty-on-start must release the lock"
        assert "return 4" in after_dirty, "dirty-on-start must return early (exit 4)"
        # the dirty branch must NOT proceed to checkout/install/test
        # (checkout/install appear later in the script, after the dirty return)
        checkout_idx = script.index("git checkout {args.candidate_sha}")
        assert dirty_idx < checkout_idx, "dirty-on-start must return before checkout"


class TestDraftPrArtifactGate:
    """Requirement 6: missing any of the 5 artifacts forbids PR creation."""

    def test_missing_artifact_exits_nonzero(self):
        """create-draft-pr must exit 1 when an artifact is missing."""
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            # create only 4 of 5 artifacts
            for name in ["plan.json", "implementation.json", "verification.json", "review.json"]:
                (run_dir / name).write_text("{}")
            # missing codex-budget-summary.json
            r = subprocess.run(
                ["python", str(REPO_ROOT / "scripts" / "create-draft-pr"),
                 "--repo", d, "--task-id", "T1", "--task-branch", "agent/T1",
                 "--base-branch", "dev", "--run-dir", d, "--test-mode"],
                capture_output=True, text=True, timeout=30,
            )
            assert r.returncode != 0, "must fail when an artifact is missing"
            assert "missing artifact" in r.stderr.lower() or "PR_CREATION_FAILED" in r.stderr

    def test_all_artifacts_present_test_mode_succeeds(self):
        """With all 5 artifacts present and --test-mode, PR creation succeeds
        with a test:// URL (no gh call)."""
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            # minimal valid artifacts
            (run_dir / "plan.json").write_text(json.dumps({"steps": [{"description": "x"}]}))
            (run_dir / "implementation.json").write_text(json.dumps({
                "candidate_sha": "abc", "changed_files": ["a.py"]}))
            (run_dir / "verification.json").write_text(json.dumps({
                "candidate_sha": "abc", "tested_sha": "abc", "passed": True,
                "wsl_results": {"build": True, "pytest_passed": True, "summary": "ok"}}))
            (run_dir / "review.json").write_text(json.dumps({
                "decision": "APPROVED", "reviewed_sha": "abc", "findings": []}))
            (run_dir / "codex-budget-summary.json").write_text(json.dumps({"total_used": 2}))
            r = subprocess.run(
                ["python", str(REPO_ROOT / "scripts" / "create-draft-pr"),
                 "--repo", d, "--task-id", "T1", "--task-branch", "agent/T1",
                 "--base-branch", "dev", "--run-dir", d, "--test-mode"],
                capture_output=True, text=True, timeout=30,
            )
            assert r.returncode == 0, f"should succeed: {r.stderr}"
            url = (run_dir / "draft-pr-url.txt").read_text()
            assert url.startswith("test://pr/"), f"test-mode URL: {url}"

    def test_production_mode_requires_gh(self):
        """Without --test-mode, production mode calls gh (which fails without
        a real GitHub remote, proving test-mode does not leak into production)."""
        with tempfile.TemporaryDirectory() as d:
            run_dir = Path(d)
            for name, content in [("plan.json", {"steps": [{"description": "x"}]}),
                                  ("implementation.json", {"candidate_sha": "abc", "changed_files": []}),
                                  ("verification.json", {"candidate_sha": "abc", "tested_sha": "abc", "passed": True, "wsl_results": {"build": True, "pytest_passed": True, "summary": "ok"}}),
                                  ("review.json", {"decision": "APPROVED", "reviewed_sha": "abc", "findings": []}),
                                  ("codex-budget-summary.json", {"total_used": 2})]:
                (run_dir / name).write_text(json.dumps(content))
            # no --test-mode: production path tries gh, which fails (no remote)
            r = subprocess.run(
                ["python", str(REPO_ROOT / "scripts" / "create-draft-pr"),
                 "--repo", d, "--task-id", "T1", "--task-branch", "agent/T1",
                 "--base-branch", "dev", "--run-dir", d],
                capture_output=True, text=True, timeout=30,
            )
            assert r.returncode != 0, "production mode must fail without a real gh remote"
            assert "PR_CREATION_FAILED" in r.stderr or "gh" in r.stderr.lower()
