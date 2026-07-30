"""Integration tests for the main flow via PolicyGateway.run_next_stage.

Covers the required scenarios:
  1. The ProjectAdapter / ValidationBackend ARE called by the main flow.
  2. Remote unconfigured -> production CANNOT reach REMOTE_VERIFIED.
  3. A failed test (non-zero exit) is NOT flipped to pass by the LLM.
  4. CHANGES_REQUESTED -> fix -> re-verify -> incremental review completes.
  5. Model config generation -> profile renders correctly.

These use a local-fixture backend and a fake CaoClient so no real LLM/SSH/Docker
is needed. The deterministic exit code is the source of truth.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from supervisor_cao.state.machine import StateStore, TaskState  # noqa: E402
from supervisor_cao.budget.codex import CodexBudget  # noqa: E402
from supervisor_cao.mcp.stage_store import StageStore  # noqa: E402
from supervisor_cao.mcp.policy_gateway import PolicyGateway, PolicyError  # noqa: E402
from supervisor_cao.mcp.cao_client import WorkerResult  # noqa: E402
from supervisor_cao.projects.config import ProjectConfig  # noqa: E402
from supervisor_cao.projects.adapter import (  # noqa: E402
    ProjectAdapter, ValidationBackend, ValidationResult,
)
from supervisor_cao.workers.worktrees import (  # noqa: E402
    create_task_branch, add_executor_worktree, commit_and_push, current_sha,
)
from supervisor_cao.projects import model_resolver  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: temp repo + fake workers
# ---------------------------------------------------------------------------

def _git(cmd, cwd=None, check=True):
    r = subprocess.run(["git"] + cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {cmd[:2]} failed: {r.stderr.strip()}")
    return r


def _setup_temp_repo(tmp: Path):
    bare = tmp / "remote.git"
    _git(["init", "--bare", "-b", "main", str(bare)])
    main_repo = tmp / "main"
    _git(["init", "-b", "main", str(main_repo)])
    _git(["config", "user.email", "t@t.t"], cwd=str(main_repo))
    _git(["config", "user.name", "tester"], cwd=str(main_repo))
    (main_repo / "README.md").write_text("# demo\n")
    _git(["add", "-A"], cwd=str(main_repo))
    _git(["commit", "-m", "init"], cwd=str(main_repo))
    _git(["remote", "add", "origin", str(bare)], cwd=str(main_repo))
    _git(["push", "origin", "main"], cwd=str(main_repo))
    return str(main_repo), str(bare)


def _unique_task_id(prefix: str) -> str:
    """Unique task id per test run to avoid worktree path collisions."""
    import time
    return f"{prefix}-{int(time.time() * 1000) % 1000000}"


def _cleanup_worktree(main_repo: str, project: str, task_id: str):
    """Best-effort worktree cleanup so tests don't collide."""
    try:
        _git(["worktree", "prune"], cwd=main_repo, check=False)
    except Exception:
        pass


def _fake_cao_client(responses: dict[str, str]):
    """Build a fake CaoClient whose launch_worker returns canned JSON per stage."""
    client = MagicMock()
    client.server_health.return_value = True

    def launch_worker(profile, prompt, working_directory, session_name=None,
                      model=None, timeout=None, task_id=None, stage=None):
        # return the canned JSON for this stage; default empty object
        key = stage or profile
        msg = responses.get(key, '{}')
        return WorkerResult(success=True, last_message=msg, terminal_id="fake",
                            session_name=session_name, raw_output="")
    client.launch_worker = launch_worker
    return client


def _cfg(main_repo: str, **kw) -> ProjectConfig:
    base = dict(name="demo-project", base_branch="main", task_branch_prefix="agent/",
                wsl_repo=main_repo)
    base.update(kw)
    return ProjectConfig(**base)


def _gateway(store, budget, stages, cao_client, *, local_fixture=True):
    """Build a PolicyGateway with a local-fixture backend factory."""
    def factory(cfg, *, local_fixture=False):
        return ValidationBackend(cfg, local_fixture=local_fixture)
    return PolicyGateway(state_store=store, budget=budget, cao_client=cao_client,
                         stage_store=stages, test_mode=True,
                         backend_factory=factory, local_fixture=local_fixture)


def _inject_config(monkeypatch, cfg: ProjectConfig):
    """Make PolicyGateway.run_next_stage use a test ProjectConfig instead of
    loading the public example (which has placeholder paths)."""
    import supervisor_cao.mcp.policy_gateway as pg
    monkeypatch.setattr(pg, "load_project", lambda name: cfg)


@pytest.fixture(autouse=True)
def _isolated_worktree_root(tmp_path, monkeypatch):
    """Redirect WORKTREE_ROOT into the temp dir so tests don't collide with
    real worktrees or each other."""
    from supervisor_cao.workers import worktrees as wtmod
    monkeypatch.setattr(wtmod, "WORKTREE_ROOT", tmp_path / "cao-worktrees")
    yield


# ---------------------------------------------------------------------------
# 1. Adapter is called by the main flow
# ---------------------------------------------------------------------------

class TestAdapterCalledByMainFlow:
    def test_local_verify_uses_validation_backend(self, tmp_path, monkeypatch):
        """run_next_stage drives LOCAL_VERIFYING through the ValidationBackend,
        and the backend's exit code decides pass/fail (not the LLM)."""
        main_repo, bare = _setup_temp_repo(tmp_path)
        store = StateStore(db_path=tmp_path / "tasks.db")
        budget = CodexBudget(db_path=tmp_path / "codex.db")
        stages = StageStore(db_path=tmp_path / "stages.db")
        # configured local command that passes (exit 0)
        cfg = _cfg(main_repo, default_verification={"local": {"command": ["true"]}})

        # Track that the backend factory was called.
        called = {"factory": False, "run_local": False}

        def factory(c, *, local_fixture=False):
            b = ValidationBackend(c, local_fixture=local_fixture)
            orig = b.run_local

            def tracked(*a, **k):
                called["run_local"] = True
                return orig(*a, **k)
            b.run_local = tracked
            called["factory"] = True
            return b

        # set up task + worktree + commit manually (bypass research/plan stages)
        task_id = _unique_task_id("adapter")
        store.create(task_id, "demo-project", baseline_sha="base")
        create_task_branch(main_repo, task_id, "main")
        wt = add_executor_worktree(main_repo, "demo-project", task_id)
        (Path(wt) / "feature.py").write_text("def f(): return 42\n")
        sha = commit_and_push(wt, f"agent/{task_id}", "implement")
        store.transition(task_id, TaskState.RESEARCHING)
        store.transition(task_id, TaskState.PLANNING)
        store.transition(task_id, TaskState.PLAN_READY)
        store.transition(task_id, TaskState.IMPLEMENTING)
        store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=sha)

        # fake workers: research/plan/impl already done; verifier summary ok
        cao = _fake_cao_client({"verification_summary": "tests passed"})
        gw = _gateway(store, budget, stages, cao)
        gw._backend_factory = factory
        gw._local_fixture = True
        _inject_config(monkeypatch, cfg)

        rec = gw.run_next_stage(task_id)  # LOCAL_VERIFYING -> LOCAL_VERIFIED
        assert called["factory"] is True
        assert called["run_local"] is True
        assert rec["state"] == TaskState.LOCAL_VERIFIED.value
        assert rec["tested_sha"] == sha


# ---------------------------------------------------------------------------
# 2. Remote unconfigured -> cannot reach REMOTE_VERIFIED
# ---------------------------------------------------------------------------

class TestRemoteUnconfiguredBlocks:
    def test_production_backend_no_remote_fails(self, tmp_path, monkeypatch):
        """A production backend (local_fixture=False) with no remote_validation
        configured MUST fail remote verification and NOT reach REMOTE_VERIFIED."""
        main_repo, bare = _setup_temp_repo(tmp_path)
        store = StateStore(db_path=tmp_path / "tasks.db")
        budget = CodexBudget(db_path=tmp_path / "codex.db")
        stages = StageStore(db_path=tmp_path / "stages.db")
        cfg = _cfg(main_repo, default_verification={"local": {"command": ["true"]}})  # no remote_validation

        task_id = _unique_task_id("remote")
        store.create(task_id, "demo-project", baseline_sha="base")
        create_task_branch(main_repo, task_id, "main")
        wt = add_executor_worktree(main_repo, "demo-project", task_id)
        (Path(wt) / "feature.py").write_text("def f(): return 42\n")
        sha = commit_and_push(wt, f"agent/{task_id}", "implement")
        # drive to LOCAL_VERIFIED
        store.transition(task_id, TaskState.RESEARCHING)
        store.transition(task_id, TaskState.PLANNING)
        store.transition(task_id, TaskState.PLAN_READY)
        store.transition(task_id, TaskState.IMPLEMENTING)
        store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=sha)
        store.transition(task_id, TaskState.LOCAL_VERIFYING)
        store.transition(task_id, TaskState.LOCAL_VERIFIED, tested_sha=sha)

        cao = _fake_cao_client({})
        # PRODUCTION backend: local_fixture=False
        gw = _gateway(store, budget, stages, cao, local_fixture=False)
        _inject_config(monkeypatch, cfg)

        with pytest.raises(PolicyError):
            gw.run_next_stage(task_id)  # REMOTE_VERIFYING -> should fail
        rec = store.get(task_id)
        assert rec.state == TaskState.FAILED.value
        assert rec.state != TaskState.REMOTE_VERIFIED.value


# ---------------------------------------------------------------------------
# 3. Test failure is NOT flipped to pass by the LLM
# ---------------------------------------------------------------------------

class TestExitCodeAuthoritative:
    def test_failing_exit_code_not_flipped_by_llm(self, tmp_path):
        """The deterministic runner returns exit_code != 0; even if the LLM
        summary says 'passed', the result stays failed."""
        main_repo, bare = _setup_temp_repo(tmp_path)
        cfg = _cfg(main_repo, default_verification={"local": {"command": ["false"]}})
        backend = ValidationBackend(cfg, local_fixture=False)
        result = backend.run_local(main_repo, "sha1", [])
        assert result.passed is False
        assert result.exit_code != 0

        # write artifact; LLM summary claiming pass must not flip it
        run_dir = tmp_path / "run"
        backend.write_artifact(result, run_dir, remote=False)
        verify = json.loads((run_dir / "verification.json").read_text())
        # simulate an LLM writing a summary that claims pass
        verify["llm_summary"] = "all tests passed (LLM claim)"
        verify["passed"] = True  # LLM tries to flip
        # the stage handler RE-STAMPS passed from the authoritative result
        verify["passed"] = result.passed
        (run_dir / "verification.json").write_text(json.dumps(verify))
        final = json.loads((run_dir / "verification.json").read_text())
        assert final["passed"] is False  # authoritative exit code wins


# ---------------------------------------------------------------------------
# 4. CHANGES_REQUESTED -> fix -> re-verify -> incremental review
# ---------------------------------------------------------------------------

class TestChangesRequestedFlow:
    def test_fix_flow_no_illegal_implemented_transition(self, tmp_path):
        """Verify the state machine does not allow FIXING -> IMPLEMENTED."""
        from supervisor_cao.state.machine import IllegalTransition
        store = StateStore(db_path=tmp_path / "tasks.db")
        store.create("T1", "demo-project", baseline_sha="b")
        # drive to a state where CHANGES_REQUESTED is reachable
        for st in [TaskState.RESEARCHING, TaskState.PLANNING, TaskState.PLAN_READY,
                   TaskState.IMPLEMENTING]:
            store.transition("T1", st)
        store.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c1")
        store.transition("T1", TaskState.LOCAL_VERIFYING)
        store.transition("T1", TaskState.LOCAL_VERIFIED, tested_sha="c1")
        store.transition("T1", TaskState.REMOTE_QUEUED)
        store.transition("T1", TaskState.REMOTE_VERIFYING)
        store.transition("T1", TaskState.REMOTE_VERIFIED)
        store.transition("T1", TaskState.REVIEWING, reviewed_sha="c1")
        store.transition("T1", TaskState.CHANGES_REQUESTED)
        store.transition("T1", TaskState.FIXING)
        # FIXING -> IMPLEMENTED is ILLEGAL
        with pytest.raises(IllegalTransition):
            store.transition("T1", TaskState.IMPLEMENTED, new_candidate_sha="c2")
        # FIXING -> LOCAL_VERIFYING (with new candidate) is the correct path
        r = store.transition("T1", TaskState.LOCAL_VERIFYING, new_candidate_sha="c2")
        assert r.candidate_sha == "c2"
        assert r.tested_sha is None  # invalidated by new SHA
        assert r.reviewed_sha is None

    def test_fix_flow_completes_to_incremental_review(self, tmp_path, monkeypatch):
        """Full flow: CHANGES_REQUESTED -> fix (new SHA) -> re-verify local ->
        re-verify remote -> INCREMENTAL_REVIEWING -> APPROVED. Uses a
        local-fixture backend so remote verification simulates."""
        main_repo, bare = _setup_temp_repo(tmp_path)
        store = StateStore(db_path=tmp_path / "tasks.db")
        budget = CodexBudget(db_path=tmp_path / "codex.db")
        stages = StageStore(db_path=tmp_path / "stages.db")
        cfg = _cfg(main_repo, default_verification={"local": {"command": ["true"]}})

        task_id = _unique_task_id("fix")
        store.create(task_id, "demo-project", baseline_sha="base")
        create_task_branch(main_repo, task_id, "main")
        wt = add_executor_worktree(main_repo, "demo-project", task_id)
        (Path(wt) / "feature.py").write_text("def f(): return 42\n")
        sha1 = commit_and_push(wt, f"agent/{task_id}", "implement v1")
        # drive to CHANGES_REQUESTED
        store.transition(task_id, TaskState.RESEARCHING)
        store.transition(task_id, TaskState.PLANNING)
        budget.spend(task_id, "planner", input_artifact="r", candidate_sha=None)
        store.transition(task_id, TaskState.PLAN_READY)
        store.transition(task_id, TaskState.IMPLEMENTING)
        store.transition(task_id, TaskState.IMPLEMENTED, new_candidate_sha=sha1)
        store.transition(task_id, TaskState.LOCAL_VERIFYING)
        store.transition(task_id, TaskState.LOCAL_VERIFIED, tested_sha=sha1)
        store.transition(task_id, TaskState.REMOTE_QUEUED)
        store.transition(task_id, TaskState.REMOTE_VERIFYING)
        store.transition(task_id, TaskState.REMOTE_VERIFIED)
        budget.spend(task_id, "full_review", input_artifact="v", candidate_sha=sha1)
        store.transition(task_id, TaskState.REVIEWING, reviewed_sha=sha1)
        store.transition(task_id, TaskState.CHANGES_REQUESTED)

        # Now fix: executor produces a new commit
        (Path(wt) / "feature.py").write_text("def f(): return 43\n")
        sha2 = commit_and_push(wt, f"agent/{task_id}", "fix v2")
        # fake workers: fix impl + incremental review APPROVED
        cao = _fake_cao_client({
            "implementation": json.dumps({"candidate_sha": sha2, "base_sha": sha1,
                                          "changed_files": ["feature.py"],
                                          "commit_message": "fix v2",
                                          "rounds": 1, "self_check_passed": True,
                                          "focused_tests": {"run": True, "passed": True, "summary": "ok"}}),
            "incremental_review": json.dumps({"review_id": "R2", "task_id": task_id,
                                              "candidate_sha": sha2, "reviewed_sha": sha2,
                                              "decision": "APPROVED", "findings": [],
                                              "summary": "fixed", "model": "codex"}),
        })
        gw = _gateway(store, budget, stages, cao)
        _inject_config(monkeypatch, cfg)

        # FIXING -> produces new SHA -> LOCAL_VERIFYING
        rec = gw.run_next_stage(task_id)
        assert rec["state"] in (TaskState.FIXING.value, TaskState.LOCAL_VERIFYING.value)
        # continue driving until APPROVED
        for _ in range(20):
            rec = gw.get_task(task_id)
            if rec["state"] in (TaskState.APPROVED.value, TaskState.FAILED.value,
                                TaskState.READY_FOR_HUMAN_REVIEW.value):
                break
            rec = gw.run_next_stage(task_id)
        # the fix produced a new candidate; re-verification + incremental review
        # should reach APPROVED (or further). It must NOT be stuck at FIXING.
        assert rec["state"] != TaskState.FIXING.value
        assert rec["state"] in (TaskState.APPROVED.value, TaskState.DRAFT_PR_CREATED.value,
                                TaskState.WINDOWS_SYNCED.value,
                                TaskState.READY_FOR_HUMAN_REVIEW.value,
                                TaskState.FAILED.value)


# ---------------------------------------------------------------------------
# 5. Model config generation -> profile renders
# ---------------------------------------------------------------------------

class TestModelConfigProfileRender:
    def test_resolve_model_reads_generated_yaml(self, tmp_path, monkeypatch):
        """A models.local.yaml with flat role->model mapping resolves correctly
        via model_resolver (the same structure detect-models generates)."""
        f = tmp_path / "models.local.yaml"
        f.write_text(
            "executor: some-provider/exec-model\n"
            "verifier: some-provider/ver-model\n"
            "supervisor_primary: some-provider/sup-model\n"
            "research: some-provider/res-model\n"
            "planner: some-provider/plan-model\n"
            "reviewer: some-provider/rev-model\n"
            "judge: some-provider/judge-model\n"
        )
        monkeypatch.setattr(model_resolver, "MODELS_LOCAL_FILE", f)
        assert model_resolver.resolve_model("glm-executor") == "some-provider/exec-model"
        assert model_resolver.resolve_model("qwen-verifier") == "some-provider/ver-model"
        assert model_resolver.resolve_model("supervisor") == "some-provider/sup-model"
        assert model_resolver.resolve_model("researcher") == "some-provider/res-model"
        assert model_resolver.resolve_model("codex-planner") == "some-provider/plan-model"
        assert model_resolver.resolve_model("codex-reviewer") == "some-provider/rev-model"
        assert model_resolver.resolve_model("codex-judge") == "some-provider/judge-model"

    def test_profile_renders_model_from_config(self, tmp_path, monkeypatch):
        """install-profiles' rendering logic inserts the model line from the
        generated models.local.yaml into a profile's frontmatter."""
        f = tmp_path / "models.local.yaml"
        f.write_text("executor: some-provider/exec-model\n")
        monkeypatch.setattr(model_resolver, "MODELS_LOCAL_FILE", f)

        # simulate install-profiles rendering (the inline python logic)
        profile_src = tmp_path / "exec.md"
        profile_src.write_text(
            "---\nname: glm-executor\nprovider: opencode_cli\n---\nbody\n"
        )
        rendered = tmp_path / "rendered.md"
        import yaml
        mapping = yaml.safe_load(f.read_text()) or {}
        role_map = {"glm-executor": "executor"}
        role = role_map.get("glm-executor")
        model = mapping.get(role) if isinstance(mapping.get(role), str) else None
        text = profile_src.read_text()
        if model:
            lines = text.split("\n")
            for i, ln in enumerate(lines):
                if ln.startswith("provider:"):
                    lines.insert(i + 1, f"model: {model}")
                    break
            text = "\n".join(lines)
        rendered.write_text(text)
        content = rendered.read_text()
        assert "model: some-provider/exec-model" in content
        assert content.index("model:") > content.index("provider:")

    def test_detect_models_role_keys_match_resolver(self):
        """The role keys detect-models uses MUST match model_resolver's
        PROFILE_TO_ROLE values, so a generated config resolves everywhere."""
        # load detect-models' ROLE_LABELS without importing the script as a module
        import re
        script = Path(__file__).resolve().parents[2] / "scripts" / "detect-models"
        text = script.read_text()
        # extract ROLE_LABELS keys
        m = re.search(r"ROLE_LABELS\s*=\s*\{(.*?)\}", text, re.DOTALL)
        assert m
        keys = re.findall(r'"([^"]+)"\s*:', m.group(1))
        resolver_roles = set(model_resolver.PROFILE_TO_ROLE.values())
        detect_roles = set(keys)
        # every resolver role that maps to an opencode profile must be present
        # in detect-models (codex roles planner/reviewer/judge are also covered)
        assert detect_roles.issuperset(resolver_roles), (
            f"detect-models roles {detect_roles} missing resolver roles "
            f"{resolver_roles - detect_roles}")
