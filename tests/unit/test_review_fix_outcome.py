"""Unit tests for the runtime review-fix outcome evaluator (Goal §5).

The evaluator is a pure function that classifies a review-fix scenario run
into three independent results — ``protocol_passed``, ``auto_fix_passed``,
``safety_behavior_passed`` — plus ``task_outcome`` and ``status``.

Goal §5 defines two passing results:

* Result A (auto-fix success): APPROVED with the full fix loop completed.
* Result B (fix insufficient, safe downgrade): NEEDS_HUMAN with a valid
  Judge UPHOLD/MIXED/UNRESOLVED ruling — the platform refused to fake APPROVED.
  The protocol still passes; ``auto_fix_passed`` is False.

These tests pin both results plus the negative cases (SKIPPED_PROTOCOL,
missing Judge ruling, FAILED, SHA mismatch, reused verification evidence).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.cli.acceptance import _evaluate_review_fix_outcome  # noqa: E402

APPROVED = "APPROVED"
NEEDS_HUMAN = "NEEDS_HUMAN"
FAILED = "FAILED"

# Distinct SHAs for the full-protocol cases.
INJECTED = "aaa111"
FIXED = "bbb222"
# When the reviewer directly approves, the "fixed" SHA equals the injected one.
SAME = INJECTED


def _common_kwargs(**overrides):
    base = dict(
        final_state=APPROVED,
        had_changes_requested=True,
        had_fix=True,
        had_incremental=True,
        first_candidate_sha=INJECTED,
        fixed_candidate_sha=FIXED,
        tested_sha=FIXED,
        reviewed_sha=FIXED,
        judge_ruling=None,
        approved_state=APPROVED,
        needs_human_state=NEEDS_HUMAN,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Result A: auto-fix success (APPROVED through the full fix loop)
# ---------------------------------------------------------------------------


class TestResultAApproved:
    def test_full_protocol_approved_passes(self):
        o = _evaluate_review_fix_outcome(**_common_kwargs(final_state=APPROVED))
        assert o["ok"] is True
        assert o["status"] == "PASS"
        assert o["protocol_passed"] is True
        assert o["auto_fix_passed"] is True
        assert o["safety_behavior_passed"] is True
        assert o["task_outcome"] == APPROVED

    def test_judge_overturn_approved_passes(self):
        # Judge OVERTURN → APPROVED; the fix loop still ran, so protocol passes.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=APPROVED, judge_ruling="OVERTURN"))
        assert o["ok"] is True
        assert o["status"] == "PASS"
        assert o["auto_fix_passed"] is True
        assert o["safety_behavior_passed"] is True


# ---------------------------------------------------------------------------
# Result B: fix insufficient, safe downgrade (NEEDS_HUMAN + valid Judge ruling)
# ---------------------------------------------------------------------------


class TestResultBNeedsHuman:
    @pytest.mark.parametrize("ruling", ["UPHOLD", "MIXED", "UNRESOLVED"])
    def test_valid_downgrade_ruling_passes(self, ruling):
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=NEEDS_HUMAN, judge_ruling=ruling))
        assert o["ok"] is True
        assert o["status"] == "PASS"
        assert o["protocol_passed"] is True
        assert o["auto_fix_passed"] is False
        assert o["safety_behavior_passed"] is True
        assert o["task_outcome"] == NEEDS_HUMAN
        assert o["judge_ruling"] == ruling

    def test_no_judge_ruling_needs_human_fails_unsafe(self):
        # NEEDS_HUMAN without a Judge ruling = platform gave up without
        # proper arbitration → unsafe → FAIL.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=NEEDS_HUMAN, judge_ruling=None))
        assert o["ok"] is False
        assert o["status"] == "FAIL"
        assert o["protocol_passed"] is True
        assert o["auto_fix_passed"] is False
        assert o["safety_behavior_passed"] is False

    def test_overturn_with_needs_human_is_unsafe(self):
        # If the state is NEEDS_HUMAN but the Judge ruling is OVERTURN,
        # something is inconsistent — OVERTURN should lead to APPROVED.
        # The evaluator treats OVERTURN as not a valid downgrade ruling.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=NEEDS_HUMAN, judge_ruling="OVERTURN"))
        assert o["ok"] is False
        assert o["safety_behavior_passed"] is False


# ---------------------------------------------------------------------------
# SKIPPED_PROTOCOL: reviewer directly APPROVED, no fix loop
# ---------------------------------------------------------------------------


class TestSkippedProtocol:
    def test_direct_approval_no_fix_loop(self):
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=APPROVED,
            had_changes_requested=False,
            had_fix=False,
            had_incremental=False,
            first_candidate_sha=SAME,
            fixed_candidate_sha=SAME,
            tested_sha=SAME,
            reviewed_sha=SAME,
        ))
        assert o["ok"] is False
        assert o["status"] == "SKIPPED_PROTOCOL"
        assert o["protocol_passed"] is False


# ---------------------------------------------------------------------------
# Negative cases: protocol or safety violations
# ---------------------------------------------------------------------------


class TestProtocolFailures:
    def test_no_changes_requested_fails(self):
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            had_changes_requested=False))
        assert o["ok"] is False
        assert o["protocol_passed"] is False

    def test_no_fix_fails(self):
        o = _evaluate_review_fix_outcome(**_common_kwargs(had_fix=False))
        assert o["ok"] is False
        assert o["protocol_passed"] is False

    def test_no_incremental_review_fails(self):
        o = _evaluate_review_fix_outcome(**_common_kwargs(had_incremental=False))
        assert o["ok"] is False
        assert o["protocol_passed"] is False

    def test_sha_not_changed_fails(self):
        # fixed == first → no new SHA produced by the fix → reused evidence.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            first_candidate_sha=SAME,
            fixed_candidate_sha=SAME,
            tested_sha=SAME,
            reviewed_sha=SAME,
        ))
        assert o["ok"] is False
        assert o["protocol_passed"] is False
        assert o["sha_changed"] is False

    def test_sha_mismatch_fails(self):
        # candidate != tested → verification evidence does not match.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            tested_sha="ccc333"))
        assert o["ok"] is False
        assert o["protocol_passed"] is False
        assert o["sha_match"] is False

    def test_failed_state_fails(self):
        o = _evaluate_review_fix_outcome(**_common_kwargs(final_state=FAILED))
        assert o["ok"] is False
        assert o["status"] == "FAIL"
        assert o["auto_fix_passed"] is False
        assert o["safety_behavior_passed"] is False


# ---------------------------------------------------------------------------
# Independence of the three results
# ---------------------------------------------------------------------------


class TestResultIndependence:
    def test_protocol_passed_can_differ_from_auto_fix(self):
        # Result B: protocol passed but auto-fix did not.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=NEEDS_HUMAN, judge_ruling="UPHOLD"))
        assert o["protocol_passed"] is True
        assert o["auto_fix_passed"] is False
        assert o["safety_behavior_passed"] is True

    def test_auto_fix_without_protocol_is_skipped(self):
        # APPROVED but no fix loop → auto_fix True, protocol False.
        o = _evaluate_review_fix_outcome(**_common_kwargs(
            final_state=APPROVED,
            had_changes_requested=False, had_fix=False, had_incremental=False,
            first_candidate_sha=SAME, fixed_candidate_sha=SAME,
            tested_sha=SAME, reviewed_sha=SAME))
        assert o["auto_fix_passed"] is True
        assert o["protocol_passed"] is False
        assert o["status"] == "SKIPPED_PROTOCOL"
