"""Keep /dev an administrator-only static help entry, not a catalog alias."""

from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.core.star.filter.command import CommandFilter


@pytest.fixture
async def help_env(env):
    env.context.get_llm_tool_manager = Mock(
        side_effect=AssertionError("Help must not read tool catalogs")
    )
    env.plugin.get_kv_data = AsyncMock(
        side_effect=AssertionError("Help must not read plugin storage")
    )
    env.plugin.put_kv_data = AsyncMock(
        side_effect=AssertionError("Help must not write plugin storage")
    )
    return env


@pytest.mark.parametrize("group", [None, "group"])
async def test_dev_shows_static_help_in_private_and_group_chat(help_env, group):
    env = help_env
    event = env.event("/dev", group=group, admin=False)
    await env.scheduler.execute(event)
    text = "".join(event.sent)
    assert text == env.main.HELP
    for command in ("/dev", "/logs", "/chatlog", "/ctx", "/term", "/restart"):
        assert command in text
    assert "/pm help" in text
    assert "已注册命令" not in text and "/dev commands" not in text
    assert "/dev tools" not in text and "/dev plugin" not in text
    env.context.get_llm_tool_manager.assert_not_called()
    env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()
    env.context.conversation_manager.get_conversation.assert_not_awaited()
    env.plugin.get_kv_data.assert_not_awaited()
    env.plugin.put_kv_data.assert_not_awaited()
    assert event.is_stopped() and event.call_llm is False and not env.model_calls


@pytest.mark.parametrize(
    "arguments",
    [
        "commands",
        "commands 2",
        "tools",
        "tools 2",
        "plugin example",
        "help",
        "--text",
        "extra",
    ],
)
async def test_dev_rejects_all_subcommands_and_parameters(help_env, arguments):
    env = help_env
    event = env.event(f"/dev {arguments}", admin=False)
    await env.scheduler.execute(event)
    text = "".join(event.sent)
    assert "用法：/dev" in text and "不接受参数" in text
    assert "/logs" not in text
    env.context.get_llm_tool_manager.assert_not_called()
    env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()
    assert event.is_stopped() and not env.model_calls


async def test_direct_help_call_accepts_only_empty_or_whitespace_arguments(help_env):
    env = help_env
    for arguments in ("", " \t\n "):
        event = env.event()
        await env.invoke("dev", event, arguments)
        assert "".join(event.sent) == env.main.HELP
        assert event.is_stopped()
    assert not env.model_calls


async def test_dev_help_is_unavailable_before_initialization(help_env):
    env = help_env
    env.plugin.ready = False
    event = env.event()
    await env.invoke("dev", event, "tools")
    assert "尚未完成初始化" in "".join(event.sent)
    assert "用法" not in "".join(event.sent)
    env.context.get_llm_tool_manager.assert_not_called()
    assert event.is_stopped() and not env.model_calls


def test_dev_registers_one_root_command_without_aliases(env):
    handlers = [
        handler
        for handler in env.registry
        if handler.handler_module_path == env.main.__name__
        and handler.handler_name == "dev"
    ]
    assert len(handlers) == 1
    commands = [
        command
        for command in handlers[0].event_filters
        if isinstance(command, CommandFilter)
    ]
    assert len(commands) == 1
    assert commands[0].get_complete_command_names() == ["dev"]
