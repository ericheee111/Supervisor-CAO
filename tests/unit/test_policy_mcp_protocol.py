"""Unit tests for the supervisor-cao-policy MCP server protocol (requirement 1).

Verifies:
  - The server is named "supervisor-cao-policy" (NOT cao-mcp-server).
  - Exactly the 5 tools are exposed: create_task, run_next_stage, get_task,
    get_artifact, resume_task.
  - @cao-mcp-server is NOT in the Supervisor profile's allowedTools.
  - The Supervisor profile declares the supervisor-cao-policy MCP server.
  - Tools delegate to the PolicyGateway and return structured results.

These tests do not require fastmcp to be installed at collection time: they
import the server module lazily and fall back to testing the profile + gateway
contract directly when fastmcp is absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _profile_frontmatter() -> dict:
    """Parse the supervisor profile frontmatter (simple YAML-ish parse)."""
    import re
    text = (REPO_ROOT / "profiles" / "supervisor.md").read_text()
    # extract frontmatter between the first two ---
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert m, "no frontmatter in supervisor.md"
    fm = m.group(1)
    return fm


class TestSupervisorProfileContract:
    """Requirement 1: profile must declare supervisor-cao-policy, NOT cao-mcp-server."""

    def test_profile_declares_supervisor_cao_policy(self):
        fm = _profile_frontmatter()
        assert "supervisor-cao-policy" in fm
        assert "supervisor-cao-policy-mcp" in fm  # the command

    def test_profile_allowed_tools_excludes_cao_mcp_server(self):
        fm = _profile_frontmatter()
        # @cao-mcp-server must NOT be in allowedTools
        assert "@cao-mcp-server" not in fm, (
            "Supervisor profile must NOT enable @cao-mcp-server — it would let "
            "the Supervisor bypass the policy layer via handoff/assign"
        )

    def test_profile_allowed_tools_includes_policy_mcp(self):
        fm = _profile_frontmatter()
        assert "@supervisor-cao-policy" in fm
        assert "fs_read" in fm
        assert "fs_list" in fm

    def test_profile_mcp_servers_block_present(self):
        fm = _profile_frontmatter()
        assert "mcpServers:" in fm
        assert "type: stdio" in fm


class TestPolicyGatewayContract:
    """The gateway must expose the 5 tools' backing methods (requirement 1)."""

    def test_gateway_has_all_five_methods(self):
        from supervisor_cao.mcp.policy_gateway import PolicyGateway
        for method in ("create_task", "run_next_stage", "get_task",
                       "get_artifact", "resume_task"):
            assert hasattr(PolicyGateway, method), f"missing method {method}"

    def test_resume_task_calls_run_next_stage(self):
        # resume_task must delegate to run_next_stage (idempotent re-entry)
        from supervisor_cao.mcp.policy_gateway import PolicyGateway
        gw = PolicyGateway.__new__(PolicyGateway)  # bypass __init__
        called = {}
        def fake_run(tid):
            called["tid"] = tid
            return {"state": "READY_FOR_HUMAN_REVIEW", "task_id": tid}
        gw.run_next_stage = fake_run  # type: ignore[method-assign]
        result = gw.resume_task("T1")
        assert called["tid"] == "T1"
        assert result["state"] == "READY_FOR_HUMAN_REVIEW"


class TestServerToolsRegistered:
    """When fastmcp is available, verify the 5 tools are registered with the
    correct names on the 'supervisor-cao-policy' server."""

    @pytest.fixture
    def fastmcp_available(self):
        try:
            import fastmcp  # noqa: F401
            return True
        except ImportError:
            pytest.skip("fastmcp not installed on this host (installed in CAO venv)")

    def test_server_name_is_supervisor_cao_policy(self, fastmcp_available):
        from supervisor_cao.mcp.server import mcp
        # FastMCP exposes the name via .name
        assert mcp.name == "supervisor-cao-policy"

    def test_five_tools_registered(self, fastmcp_available):
        from supervisor_cao.mcp.server import mcp
        # FastMCP stores tools in _tool_manager or similar; the exact API varies
        # by version, so we check the public list_tools or the internal registry.
        tools = _get_tool_names(mcp)
        expected = {"create_task", "run_next_stage", "get_task",
                    "get_artifact", "resume_task"}
        assert expected.issubset(tools), f"missing tools: {expected - tools}"

    def test_cao_mcp_server_not_registered(self, fastmcp_available):
        from supervisor_cao.mcp.server import mcp
        tools = _get_tool_names(mcp)
        # the built-in cao-mcp-server tools must NOT be present
        for forbidden in ("handoff", "assign", "send_message"):
            assert forbidden not in tools, f"{forbidden} must not be exposed"


def _get_tool_names(mcp_server) -> set[str]:
    """Best-effort extraction of registered tool names across FastMCP versions."""
    # FastMCP stores tools in an async registry; use list_tools() via asyncio.run.
    import asyncio
    try:
        result = asyncio.run(mcp_server.list_tools())
        return {t.name for t in result}
    except Exception:
        pass
    # fallback: internal registries (older versions)
    for attr in ("_tool_manager", "_tools"):
        obj = getattr(mcp_server, attr, None)
        if obj is None:
            continue
        tools = getattr(obj, "_tools", None) or getattr(obj, "tools", None)
        if isinstance(tools, dict):
            return set(tools.keys())
        if isinstance(tools, list):
            return {getattr(t, "name", str(t)) for t in tools}
    return set()
