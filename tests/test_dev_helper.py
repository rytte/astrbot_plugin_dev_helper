"""Behavioral tests for diagnostics and authorization on the standard pipeline."""

import functools
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot_plugin_dev_helper.display import history_text, redact


def output(event):
    return "".join(event.sent)


async def test_real_pipeline_dispatches_help_and_root_subcommands(env):
    for command, expected in (
        ("/inspect", "开发助手"),
        ("/inspect commands", "命令目录"),
        ("/inspect tools", "模型工具目录"),
        ("/chatlog", "没有选中的对话"),
        ("/logs warning 2", "缓存"),
    ):
        event = env.event(command, admin=False)
        await env.scheduler.execute(event)
        assert expected in output(event)
        assert event.is_stopped()
    assert not env.model_calls


async def test_initialization_does_not_access_plugin_storage(env):
    plugin = env.main.Main(env.context, env.plugin_config)
    plugin.get_kv_data = AsyncMock(side_effect=AssertionError("Unexpected KV read"))
    plugin.put_kv_data = AsyncMock(side_effect=AssertionError("Unexpected KV write"))
    await plugin.initialize()
    assert plugin.ready
    plugin.get_kv_data.assert_not_awaited()
    plugin.put_kv_data.assert_not_awaited()


async def test_uninitialized_plugin_only_rejects_its_own_commands(env):
    env.plugin.ready = False
    ordinary = env.event("hello", user="member", admin=False)
    await env.scheduler.execute(ordinary)
    assert env.model_calls == [ordinary]
    event = env.event("/chatlog", admin=False)
    await env.scheduler.execute(event)
    assert "尚未完成初始化" in output(event)
    env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()
    assert env.model_calls == [ordinary]


@pytest.mark.parametrize("command", ["ban", "history"])
async def test_removed_command_is_available_to_other_plugins(env, command):
    async def other_command(self, event):
        await event.send(event.plain_result("Handled by another plugin"))
        event.stop_event()

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        f"other_{command}",
        "other_command",
        "other.main",
        other_command,
        [],
    )
    handler.event_filters = [CommandFilter(command, None, handler)]
    handler.handler = functools.partial(other_command, None)
    env.registry.append(handler)
    env.owners["other.main"] = StarMetadata(name="other", activated=True)
    await env.plugin.initialize()
    event = env.event(f"/{command}", user="member", admin=False)
    await env.scheduler.execute(event)
    assert output(event) == "Handled by another plugin"
    assert not env.model_calls


@pytest.mark.parametrize(
    "command",
    [
        "/inspect",
        "/logs 1",
        "/chatlog 1",
        "/logs-pic 1",
        "/chatlog-pic 1",
        "/ctx",
        "/ctx-pic",
        "/term ls",
    ],
)
async def test_non_admin_is_denied_by_real_pipeline(env, command):
    event = env.event(command, user="member", admin=False)
    await env.scheduler.execute(event)
    assert "权限" in output(event)
    env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()


@pytest.mark.parametrize(
    "method",
    ["inspect", "logs", "chatlog", "logs_pic", "chatlog_pic", "ctx", "ctx_pic", "term"],
)
async def test_direct_calls_and_api_role_cannot_bypass_authorization(env, method):
    event = env.event(user="member", admin=False)
    await env.invoke(method, event, "bad extra arguments")
    assert "仅限" in output(event)
    event = env.event()
    event.set_extra("_api_key_allow_admin_role", False)
    await env.invoke(method, event, "")
    assert "仅限" in output(event)


@pytest.mark.parametrize(
    "method,arguments",
    [
        ("logs", "--level ERROR"),
        ("logs", "level 3"),
        ("logs", "warning 2 extra"),
        ("logs", "0"),
        ("logs", "101"),
        ("logs", "1.5"),
        ("chatlog", "list"),
        ("chatlog", "show cid"),
        ("chatlog", "-1"),
        ("chatlog", "1000"),
        ("chatlog", "20 extra"),
        ("ctx", "0"),
        ("ctx", "101"),
        ("ctx", "5 extra"),
        ("inspect", "plugins"),
    ],
)
async def test_unknown_or_discarded_syntax_is_rejected(env, method, arguments):
    event = env.event()
    await env.invoke(method, event, arguments)
    assert any(word in output(event) for word in ("用法", "参数"))
    env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()


async def test_logs_filter_before_count_and_sanitize(env):
    env.broker.log_cache.clear()
    for index, level in enumerate(
        ["WARNING", "INFO", "ERROR", "DEBUG", "CRITICAL", "INFO"]
    ):
        env.broker.publish(
            {
                "level": level,
                "time": 1000 + index,
                "data": f"entry-{index} api_key=hidden-{index}",
            }
        )
    event = env.event()
    await env.invoke("logs", event, "warning 2")
    text = output(event)
    assert "entry-2" in text and "entry-4" in text
    assert "entry-0" not in text and "entry-5" not in text
    assert text.index("entry-2") < text.index("entry-4")
    assert "hidden-" not in text
    event = env.event()
    await env.invoke("logs", event, "2")
    assert "entry-4" in output(event) and "entry-5" in output(event)
    event = env.event(group="group")
    await env.invoke("logs", event, "warning")
    assert "私聊" in output(event)
    assert "entry-" not in output(event)


async def test_logs_empty_no_match_and_unavailable_are_distinct(env, monkeypatch):
    env.broker.log_cache.clear()
    event = env.event()
    await env.invoke("logs", event, "")
    assert "缓存为空" in output(event)
    env.broker.publish({"level": "INFO", "time": 1000, "data": "nothing wrong"})
    event = env.event()
    await env.invoke("logs", event, "warning")
    assert "无 WARNING" in output(event)
    monkeypatch.setattr(env.main.logging.getLogger("astrbot"), "handlers", [])
    event = env.event()
    await env.invoke("logs", event, "")
    assert "日志源不可用" in output(event)


async def test_chatlog_tail_preserves_tool_roles_and_current_group_session(env):
    event = env.event(group="group")
    records = [
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "function": {
                        "name": "demo",
                        "arguments": '{"password":"sensitive"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]
    env.context.conversation_manager.get_curr_conversation_id.return_value = "cid"
    env.context.conversation_manager.get_conversation.return_value = SimpleNamespace(
        user_id=event.unified_msg_origin,
        platform_id="qq_one",
        history=json.dumps(records),
    )
    await env.invoke("chatlog", event, "2")
    text = output(event)
    assert "共 3 条，显示最近 2 条" in text
    assert "old" not in text and "sensitive" not in text
    assert text.index("assistant") < text.index("#3 tool")
    assert "tool_call_id" in text and "call_1" in text
    env.context.conversation_manager.get_conversation.assert_awaited_once_with(
        event.unified_msg_origin, "cid", create_if_not_exists=False
    )


async def test_chatlog_rejects_mismatched_owner_and_corrupt_records(env):
    env.context.conversation_manager.get_curr_conversation_id.return_value = "cid"
    event = env.event()
    conversation = SimpleNamespace(
        user_id="another session",
        platform_id="qq_one",
        history='[{"role":"user","content":"private"}]',
    )
    env.context.conversation_manager.get_conversation.return_value = conversation
    await env.invoke("chatlog", event, "")
    assert "归属" in output(event) and "private" not in output(event)
    conversation.user_id = event.unified_msg_origin
    for history in ("broken", "{}", "[null]", '[{"content":"no role"}]'):
        conversation.history = history
        event = env.event()
        await env.invoke("chatlog", event, "")
        assert "格式错误" in output(event)


async def test_chatlog_empty_and_missing_do_not_create(env):
    event = env.event()
    await env.invoke("chatlog", event, "")
    env.context.conversation_manager.get_conversation.assert_not_awaited()
    env.context.conversation_manager.get_curr_conversation_id.return_value = "cid"
    env.context.conversation_manager.get_conversation.return_value = None
    event = env.event()
    await env.invoke("chatlog", event, "")
    assert "记录不存在" in output(event)
    env.context.conversation_manager.get_conversation.return_value = SimpleNamespace(
        user_id=event.unified_msg_origin, platform_id="qq_one", history="[]"
    )
    event = env.event()
    await env.invoke("chatlog", event, "")
    assert "当前对话记录为空" in output(event)


async def test_catalog_counts_builtin_disabled_tools_and_paginates(env):
    env.manager.func_list = [
        FunctionTool(
            name=f"demo_{index:02d}",
            description="desc",
            parameters={},
            handler_module_path="demo.main",
            active=index != 0,
        )
        for index in range(23)
    ]
    env.manager.iter_builtin_tools = lambda: [
        FunctionTool(name="builtin", description="builtin", parameters={})
    ]
    event = env.event()
    await env.invoke("inspect", event, "")
    assert "模型工具：24 个" in output(event)
    event = env.event()
    await env.invoke("inspect", event, "tools 1")
    assert "已停用" in output(event) and "下一页：/inspect tools 2" in output(event)
    event = env.event()
    await env.invoke("inspect", event, "tools 2")
    assert "第 2/2 页" in output(event) and "demo_22" in output(event)


async def test_group_aliases_permission_and_parent_disabled_state(env):
    async def action(self, event, count: int):
        raise AssertionError("Catalogs must never invoke commands")

    parent = StarHandlerMetadata(
        EventType.AdapterMessageEvent, "demo_group", "group", "demo.main", action, []
    )
    group = CommandGroupFilter("manage", alias={"m"})
    parent.event_filters = [group, PermissionTypeFilter(PermissionType.ADMIN)]
    parent.enabled = False
    child = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "demo_action",
        "action",
        "demo.main",
        action,
        [],
        desc="Example",
    )
    command = CommandFilter("echo", {"say"}, child, group.get_complete_command_names())
    child.event_filters = [command]
    env.registry.append(parent)
    env.registry.append(child)
    env.owners["demo.main"] = StarMetadata(name="demo", activated=True, reserved=True)
    entry = next(
        item for item in env.catalog.command_entries() if item.name == "manage echo"
    )
    assert entry.source == "内置" and "停用" in entry.status
    assert "管理员" in entry.details and "m say" in entry.details


@pytest.mark.parametrize(
    "command", ["logs", "logs-pic", "chatlog-pic", "ctx", "ctx-pic"]
)
async def test_command_conflicts_fail_initialization_and_diagnostic_execution(
    env, command
):
    async def colliding(self, event):
        raise AssertionError("Must not dispatch this handler during the test")

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "other_collision",
        "collision",
        "other.main",
        colliding,
        [],
    )
    handler.event_filters = [CommandFilter("other", {command}, handler)]
    env.registry.append(handler)
    plugin = env.main.Main(env.context, env.plugin_config)
    with pytest.raises(RuntimeError, match="命令冲突"):
        await plugin.initialize()
    event = env.event()
    await env.invoke("logs", event, "")
    assert "命令冲突" in output(event)


def test_redaction_and_media_summary_do_not_destroy_normal_text():
    assert redact({"nested": {"api_key": "secret", "token_usage": 25}}) == {
        "nested": {"api_key": "[已隐藏]", "token_usage": 25}
    }
    for text, secret in (
        ('{"password": "hello world"}', "hello world"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("https://user:pass@host/path?key=xyz", "pass"),
        ("access_token=xyz", "xyz"),
    ):
        assert secret not in redact(text)
    assert redact("普通聊天内容") == "普通聊天内容"
    text = history_text(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "private-image-location"}},
            ],
        }
    )
    assert "hi" in text and "多媒体" in text and "private-image-location" not in text


async def test_real_builtin_and_mcp_tool_metadata_are_listed_without_calls(env):
    import mcp.types
    from astrbot.core.agent.mcp_client import MCPTool
    from astrbot.core.provider.func_tool_manager import FunctionToolManager

    manager = FunctionToolManager()
    client = SimpleNamespace(call_tool_with_reconnect=AsyncMock())
    tool = MCPTool(
        mcp.types.Tool(
            name="search.demo", description="MCP search", inputSchema={"type": "object"}
        ),
        client,
        "demo_server",
    )
    manager.func_list.append(tool)
    entries = env.catalog.tool_entries(manager)
    assert any(entry.source == "内置" for entry in entries)
    assert any(
        entry.source == "MCP"
        and entry.plugin == "demo_server"
        and entry.name == tool.name
        for entry in entries
    )
    client.call_tool_with_reconnect.assert_not_awaited()


async def test_current_session_isolation_is_resolved_by_real_waking_stage(env):
    env.waking.unique_session = True
    event = env.event("/chatlog 1", user="admin", group="group", admin=False)
    origin = "qq_one:GroupMessage:admin_group"
    manager = env.context.conversation_manager
    manager.get_curr_conversation_id.return_value = "isolated_cid"
    manager.get_conversation.return_value = SimpleNamespace(
        user_id=origin,
        platform_id="qq_one",
        history='[{"role":"user","content":"isolated"}]',
    )
    await env.scheduler.execute(event)
    manager.get_curr_conversation_id.assert_awaited_once_with(origin)
    assert origin in output(event) and "isolated" in output(event)


async def test_long_chatlog_keeps_complete_selected_entries_and_redacts_secrets(env):
    event = env.event()
    content = "very long " * 2000 + "message tail api_key=long-message-secret"
    manager = env.context.conversation_manager
    manager.get_curr_conversation_id.return_value = "cid"
    manager.get_conversation.return_value = SimpleNamespace(
        user_id=event.unified_msg_origin,
        platform_id="qq_one",
        history=json.dumps([{"role": "user", "content": content} for _ in range(3)]),
    )
    await env.invoke("chatlog", event, "3")
    text = output(event)
    assert all(f"#{i} user" in text for i in range(1, 4))
    assert len(text) > 60000
    assert text.count("very long " * 2000 + "message tail api_key=[已隐藏]") == 3
    assert "long-message-secret" not in text and "已截断" not in text


@pytest.mark.parametrize("arguments", ["2", "warning 2"])
async def test_long_logs_keep_complete_bodies_and_spacing(env, arguments):
    first = "[18:11:54] WARNING " + "log body " * 1800
    second = "[18:11:55] ERROR Traceback:\n\n  " + "frame " * 1600
    env.broker.log_cache.clear()
    for level, data in (
        ("INFO", "older record"),
        ("WARNING", first + "first tail api_key=first-secret\n\n"),
        ("ERROR", second + "last tail password=second-secret\n"),
    ):
        env.broker.publish({"level": level, "time": 1000, "data": data})
    event = env.event()
    await env.invoke("logs", event, arguments)
    text = output(event)
    expected_body = (
        first + "first tail api_key=[已隐藏]\n" + second + "last tail password=[已隐藏]"
    )
    assert text.endswith("\n\n" + expected_body)
    assert len(text) > 14000
    assert "first-secret" not in text and "second-secret" not in text
    assert "older record" not in text and "已截断" not in text
