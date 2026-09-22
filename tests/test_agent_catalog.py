"""Verify Agent tool discovery against AstrBot's current registration API."""

import functools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types
from astrbot.core.agent.agent import Agent
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.provider.func_tool_manager import FunctionToolManager
from astrbot.core.star.register import star_handler as registration
from astrbot.core.star.star import StarMetadata


async def test_decorator_registered_agent_and_child_appear_in_plugin_details(
    env, monkeypatch
):
    manager = FunctionToolManager()
    monkeypatch.setattr(registration, "llm_tools", manager)
    env.context.provider_manager.llm_tools = manager
    env.owners[__name__] = StarMetadata(
        name="agent_plugin", module_path=__name__, activated=True
    )

    async def delegate(self, event):
        raise AssertionError("Catalog must not invoke the Agent")

    agent = registration.register_agent("helper", "Help with lookup")(delegate)

    @agent.llm_tool(name="agent_lookup")
    async def lookup(self, event, query: str):
        """Look up a result.

        Args:
            query (string): Query text.
        """
        raise AssertionError("Catalog must not execute tools")

    handoff = manager.func_list[0]
    child = handoff.agent.tools[0]
    # The core binds concrete child callbacks; the handoff keeps its callback.
    child.handler_module_path = __name__
    child.handler = functools.partial(child.handler, object())
    entries = env.catalog.tool_entries(manager)
    owned = {entry.name: entry for entry in entries if entry.plugin == "agent_plugin"}
    assert set(owned) == {"transfer_to_helper", "agent_lookup"}
    assert "transfer_to_helper" in owned["agent_lookup"].details
    assert "query" in owned["agent_lookup"].details
    event = env.event("/inspect plugin agent_plugin", admin=False)
    await env.scheduler.execute(event)
    text = "".join(event.sent)
    assert "agent_lookup" in text and "transfer_to_helper" in text
    assert "没有已注册" not in text
    assert not env.model_calls


def test_nested_shared_and_cyclic_agents_preserve_distinct_tool_objects(env):
    shared = FunctionTool(name="lookup", description="shared", parameters={})
    other = FunctionTool(name="lookup", description="other", parameters={})
    outer = HandoffTool(Agent(name="outer", tools=[shared]))
    inner = HandoffTool(Agent(name="inner", tools=[shared, other, outer]))
    outer.agent.tools.append(inner)
    env.manager.func_list = [outer, shared]
    entries = env.catalog.tool_entries(env.manager)
    assert len(entries) == 4
    lookups = [entry for entry in entries if entry.name == "lookup"]
    assert len(lookups) == 2
    shared_entry = next(entry for entry in lookups if "说明：shared" in entry.details)
    assert "transfer_to_outer" in shared_entry.details
    assert "transfer_to_inner" in shared_entry.details
    assert outer.agent.tools == [shared, inner]
    assert inner.agent.tools == [shared, other, outer]


def test_agent_children_keep_their_own_sources_and_disabled_states(env):
    client = SimpleNamespace(call_tool_with_reconnect=AsyncMock())
    mcp_tool = MCPTool(
        mcp.types.Tool(name="remote_lookup", inputSchema={"type": "object"}),
        client,
        "remote_server",
    )
    child = FunctionTool(
        name="disabled_lookup",
        description="disabled",
        parameters={},
        handler_module_path="child_plugin.tools",
        active=False,
    )
    env.owners["child_plugin.main"] = StarMetadata(
        name="child_plugin", module_path="child_plugin.main", activated=True
    )
    env.owners["parent_plugin.main"] = StarMetadata(
        name="parent_plugin", module_path="parent_plugin.main", activated=False
    )
    parent = HandoffTool(
        Agent(name="helper", tools=[child, mcp_tool]),
        handler_module_path="parent_plugin.main",
    )
    env.manager.func_list = [parent]
    entries = {entry.name: entry for entry in env.catalog.tool_entries(env.manager)}
    assert entries["transfer_to_helper"].plugin == "parent_plugin"
    assert entries["transfer_to_helper"].status == "已停用"
    assert entries["disabled_lookup"].plugin == "child_plugin"
    assert entries["disabled_lookup"].status == "已停用"
    assert entries["remote_lookup"].source == "MCP"
    assert entries["remote_lookup"].plugin == "remote_server"
    client.call_tool_with_reconnect.assert_not_awaited()


def test_agent_string_references_and_dynamic_tool_scope_are_explicit(env):
    manager = FunctionToolManager()
    shared = FunctionTool(name="lookup", description="shared", parameters={})
    parent = HandoffTool(Agent(name="helper", tools=["lookup", "unregistered_tool"]))
    dynamic = HandoffTool(Agent(name="dynamic", tools=None))
    manager.func_list = [parent, shared, dynamic]
    entries = env.catalog.tool_entries(manager)
    lookup_entries = [entry for entry in entries if entry.name == "lookup"]
    assert len(lookup_entries) == 1
    assert "transfer_to_helper" in lookup_entries[0].details
    parent_entry = next(entry for entry in entries if entry.name == parent.name)
    assert "工具引用信息不可用" in parent_entry.details
    assert "unregistered_tool" in parent_entry.details
    dynamic_entry = next(entry for entry in entries if entry.name == dynamic.name)
    assert "按请求决定" in dynamic_entry.details
