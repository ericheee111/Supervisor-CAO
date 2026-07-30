"""Project configuration loader (spec §13, §19).

Project config = project defaults + task-level overrides. Never hard-code a
project name. Platform stays generic; adding a project = add config + validation
plugin + optional prompts.

Config resolution order (later wins):
  1. builtin defaults
  2. <repo>/config/examples/<project>.example.yaml  (public, sanitized)
  3. ~/.config/supervisor-cao/projects/<project>.local.yaml (private: real paths/hosts)
  4. task-level override file passed to `run`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config" / "examples"
LOCAL_CONFIG_DIR = Path.home() / ".config" / "supervisor-cao" / "projects"


@dataclass
class ProjectConfig:
    name: str
    base_branch: str = "main"
    task_branch_prefix: str = "agent/"
    wsl_repo: str = ""              # Linux fs path to the project's main clone
    windows_repo: str = ""          # Windows path (private, set in local config)
    remote_validation: dict = field(default_factory=dict)  # SSH host, containers (private)
    default_verification: dict = field(default_factory=dict)
    executor_limits: dict = field(default_factory=dict)
    codex_budget: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)

    @property
    def task_branch(self) -> str:
        return self.task_branch_prefix

    def task_branch_for(self, task_id: str) -> str:
        return f"{self.task_branch_prefix}{task_id}"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# Task-level overrides may ONLY touch test/benchmark/acceptance fields.
# Repository paths, SSH, containers, and base branch are project-level and
# must NOT be overridable per-task (spec §6, §13).
ALLOWED_TASK_OVERRIDE_KEYS = {
    "baseline_sha", "benchmark_selector", "performance_acceptance",
    "regression_threshold", "required_test_scope", "default_verification",
}
# Keys that must NEVER appear in a task override (security: prevents
# redirecting to a different repo/SSH/container).
FORBIDDEN_TASK_OVERRIDE_KEYS = {
    "wsl_repo", "windows_repo", "remote_validation", "base_branch",
    "task_branch_prefix", "codex_budget", "executor_limits",
}


def _filter_task_override(raw: dict) -> dict:
    """Filter a task override to only allowed keys. Rejects forbidden keys."""
    filtered = {}
    for k, v in raw.items():
        if k in FORBIDDEN_TASK_OVERRIDE_KEYS:
            raise ValueError(
                f"task override may not set '{k}' — this is a project-level field "
                f"that cannot be overridden per-task (security: spec §6)")
        if k in ALLOWED_TASK_OVERRIDE_KEYS or k == "name":
            filtered[k] = v
        # silently ignore unknown keys (forward-compat)
    return filtered


def load_project(name: str, task_override_path: str | Path | None = None) -> ProjectConfig:
    """Load a project config by name, merging public example + private local + task override."""
    cfg: dict[str, Any] = {"name": name}
    # 1. public sanitized example
    example = REPO_CONFIG_DIR / f"{name}.example.yaml"
    if example.exists():
        cfg = _deep_merge(cfg, yaml.safe_load(example.read_text()) or {})
    # 2. private local (real hosts/paths)
    local = LOCAL_CONFIG_DIR / f"{name}.local.yaml"
    if local.exists():
        cfg = _deep_merge(cfg, yaml.safe_load(local.read_text()) or {})
    # 3. task-level override (FILTERED: only test/benchmark/acceptance fields)
    if task_override_path:
        tp = Path(task_override_path)
        if tp.exists():
            raw_override = yaml.safe_load(tp.read_text()) or {}
            filtered = _filter_task_override(raw_override)
            cfg = _deep_merge(cfg, filtered)
    return ProjectConfig(
        name=cfg.get("name", name),
        base_branch=cfg.get("base_branch", "main"),
        task_branch_prefix=cfg.get("task_branch_prefix", "agent/"),
        wsl_repo=cfg.get("wsl_repo", ""),
        windows_repo=cfg.get("windows_repo", ""),
        remote_validation=cfg.get("remote_validation", {}),
        default_verification=cfg.get("default_verification", {}),
        executor_limits=cfg.get("executor_limits", {}),
        codex_budget=cfg.get("codex_budget", {}),
        extra=cfg.get("extra", {}),
    )


def list_known_projects() -> list[str]:
    """Return project names that have a public example config."""
    if not REPO_CONFIG_DIR.exists():
        return []
    return sorted(p.stem.replace(".example", "") for p in REPO_CONFIG_DIR.glob("*.example.yaml"))
