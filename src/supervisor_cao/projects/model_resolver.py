"""Model ID resolution from local config (spec §19).

Model IDs are NEVER hard-coded in the platform. They are read from the local
(gitignored) ``~/.config/supervisor-cao/models.local.yaml``, produced by
``scripts/detect-models``. The file maps role keys to ``provider/model`` ids.

If the file is absent (e.g. a fresh checkout, or CI), resolution returns None
and the CAO provider applies its own default. The platform never bakes a
specific model id into source.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MODELS_LOCAL_FILE = Path.home() / ".config" / "supervisor-cao" / "models.local.yaml"

# Profile name -> role key in models.local.yaml. This mapping is structural
# (which profile uses which role slot), NOT a model id.
PROFILE_TO_ROLE = {
    "researcher": "research",
    "glm-executor": "executor",
    "qwen-verifier": "verifier",
    "supervisor": "supervisor_primary",
    "codex-planner": "planner",
    "codex-reviewer": "reviewer",
    "codex-judge": "judge",
}


def _load_mapping() -> dict[str, Any]:
    if not MODELS_LOCAL_FILE.exists():
        return {}
    try:
        data = yaml.safe_load(MODELS_LOCAL_FILE.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_model(profile: str) -> str | None:
    """Resolve the model id for a profile from models.local.yaml.

    Returns None if no mapping is configured (the CAO provider then applies its
    own default). Never raises.
    """
    role = PROFILE_TO_ROLE.get(profile)
    if not role:
        return None
    mapping = _load_mapping()
    # Support both flat {role: "provider/model"} and nested {roles: {role: ...}}
    if role in mapping and isinstance(mapping[role], str):
        return mapping[role]
    roles = mapping.get("roles") or mapping.get("models") or {}
    if isinstance(roles, dict) and isinstance(roles.get(role), str):
        return roles[role]
    return None
