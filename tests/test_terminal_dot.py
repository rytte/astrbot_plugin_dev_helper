"""Verify opt-in terminal shortcuts through AstrBot's real message pipeline."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot_plugin_dev_helper.pictures import PicturePages, RenderError, build_html
from astrbot_plugin_dev_helper.terminal import TerminalResult, TerminalRunner


@pytest.fixture
def dot_env(env, tmp_path, monkeypatch):
    env.clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        env.main, "time", SimpleNamespace(monotonic=lambda: env.clock.now)
    )
    monkeypatch.setattr(env.main, "session_workspace", AsyncMock(return_value=tmp_path))
    env.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=object())
    )
    env.plugin.picture_renderer.render = AsyncMock(
        return_value=PicturePages([b"png"], 1)
    )

    def executed(key, root, command):
        return TerminalResult(command, root, root, "sh", "example", 0, 0.25)

    env.plugin.terminal.execute = AsyncMock(side_effect=executed)
    return env


async def dispatch(env, text, **options):
    options.setdefault("admin", False)
    event = env.event(text, **options)
    await env.scheduler.execute(event)
    return event


def register_dot_command(env, *, alias=False, active=True, enabled=True):
    calls = []

    async def other(event):
        calls.append(event)

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "other_dot",
        "other",
        "other.main",
        other,
        [],
        enabled=enabled,
    )
    handler.event_filters = [
        CommandFilter("other" if alias else ".ls", {".ls"} if alias else set(), handler)
    ]
    env.registry.append(handler)
    env.owners["other.main"] = StarMetadata(name="other", activated=active)
    return calls


async def test_enter_adds_shortcut_without_replacing_term_and_exit_removes_it(dot_env):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    assert "".join(entered.sent) == (
        "点号模式已开启：.ls → /term ls。\n"
        "10 分钟无终端操作自动退出；/term exit 手动退出。"
    )
    assert entered.is_stopped()
    env.plugin.terminal.execute.assert_not_awaited()
    env.plugin.picture_renderer.render.assert_not_awaited()
    for text in (".ls", "/term pwd"):
        event = await dispatch(env, text)
        assert isinstance(event.sent_chains[0].chain[0], Image)
        document = env.plugin.picture_renderer.render.call_args.args[0]
        assert "点号模式" in document.footer.details
        assert event.is_stopped() and not event.call_llm
    assert [call.args[2] for call in env.plugin.terminal.execute.await_args_list] == [
        "ls",
        "pwd",
    ]
    exited = await dispatch(env, "/term exit")
    assert "点号模式已关闭" in "".join(exited.sent)
    assert not env.plugin.terminal_dot_mode_active(exited)
    await dispatch(env, ".ls")
    assert env.plugin.terminal.execute.await_count == 2
    assert len(env.model_calls) == 1
    await dispatch(env, "/term pwd")
    document = env.plugin.picture_renderer.render.call_args.args[0]
    assert "点号模式" not in document.footer.details


@pytest.mark.parametrize(
    "text,command",
    [
        (".exit", "exit"),
        (".enter", "enter"),
        (". ./script.sh", "./script.sh"),
        ("..\\script.ps1", ".\\script.ps1"),
        (
            ".printf 'two  spaces'\n  printf '\tvalue'",
            "printf 'two  spaces'\n  printf '\tvalue'",
        ),
    ],
)
async def test_dot_only_removes_one_prefix_and_does_not_parse_controls(
    dot_env, text, command
):
    env = dot_env
    await dispatch(env, "/term enter")
    event = await dispatch(env, text)
    assert env.plugin.terminal.execute.call_args.args[2] == command
    assert env.plugin.terminal_dot_mode_active(event)
    assert not env.model_calls


@pytest.mark.parametrize("text", ["普通聊天", "hello .ls", "/chatlog --text", "/logs --text"])
async def test_chat_and_other_commands_do_not_execute_or_extend_mode(dot_env, text):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    key = (entered.unified_msg_origin, entered.get_sender_id())
    deadline = env.plugin._terminal_dot_modes[key]
    env.clock.now += 200
    event = await dispatch(env, text)
    env.plugin.terminal.execute.assert_not_awaited()
    assert env.plugin._terminal_dot_modes[key] == deadline
    assert bool(env.model_calls) == (not text.startswith("/"))
    assert env.plugin.terminal_dot_mode_active(event)


@pytest.mark.parametrize("enabled", [False, True])
async def test_inactive_shortcut_does_not_wake_prefix_required_chat(dot_env, enabled):
    env = dot_env
    if enabled:
        await dispatch(env, "/term enter")
        env.clock.now += 600
    env.waking.friend_message_needs_wake_prefix = True
    event = await dispatch(env, ".ls")
    env.plugin.terminal.execute.assert_not_awaited()
    assert not event.is_wake and not event.sent_chains and not env.model_calls


@pytest.mark.parametrize(
    "options",
    [
        {"platform": "another_platform"},
        {"user": "another_admin"},
        {"user": "member"},
        {"group": "group"},
    ],
)
async def test_shortcut_is_private_and_isolated_by_session_and_administrator(
    dot_env, options
):
    env = dot_env
    env.config["admins_id"].append("another_admin")
    entered = await dispatch(env, "/term enter")
    await dispatch(env, ".ls", **options)
    env.plugin.terminal.execute.assert_not_awaited()
    assert env.plugin.terminal_dot_mode_active(entered)


@pytest.mark.parametrize("command", ["enter", "exit"])
@pytest.mark.parametrize("options", [{"user": "member"}, {"group": "group"}])
async def test_mode_controls_require_admin_private_chat(dot_env, command, options):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    event = await dispatch(env, f"/term {command}", **options)
    assert any(word in "".join(event.sent) for word in ("权限", "私聊"))
    assert env.plugin.terminal_dot_mode_active(entered)
    env.plugin.terminal.execute.assert_not_awaited()
    assert len(env.plugin._terminal_dot_modes) == 1


async def test_two_administrators_in_same_session_have_independent_modes(dot_env):
    env = dot_env
    env.config["admins_id"].append("another_admin")
    entered = await dispatch(env, "/term enter")
    other = env.event(".ls", user="another_admin", admin=False)
    other.unified_msg_origin = entered.unified_msg_origin
    await env.scheduler.execute(other)
    env.plugin.terminal.execute.assert_not_awaited()
    enter_other = env.event("/term enter", user="another_admin", admin=False)
    enter_other.unified_msg_origin = entered.unified_msg_origin
    await env.scheduler.execute(enter_other)
    assert len(env.plugin._terminal_dot_modes) == 2
    exit_other = env.event("/term exit", user="another_admin", admin=False)
    exit_other.unified_msg_origin = entered.unified_msg_origin
    await env.scheduler.execute(exit_other)
    assert not env.plugin.terminal_dot_mode_active(exit_other)
    assert env.plugin.terminal_dot_mode_active(entered)
    await dispatch(env, ".ls")
    env.plugin.terminal.execute.assert_awaited_once()


async def test_slash_prefixed_dot_text_is_not_a_terminal_shortcut(dot_env):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    await dispatch(env, "/.ls")
    env.plugin.terminal.execute.assert_not_awaited()
    assert len(env.model_calls) == 1
    assert env.plugin.terminal_dot_mode_active(entered)


async def test_api_role_cannot_enter_or_use_active_shortcut(dot_env):
    env = dot_env
    for text in ("/term enter", "/term exit"):
        event = env.event(text)
        event.set_extra("_api_key_allow_admin_role", False)
        await env.invoke("term", event, text[6:])
        assert "管理员" in "".join(event.sent)
    assert not env.plugin._terminal_dot_modes
    entered = await dispatch(env, "/term enter")
    event = env.event(".ls")
    event.set_extra("_api_key_allow_admin_role", False)
    await env.scheduler.execute(event)
    env.plugin.terminal.execute.assert_not_awaited()
    assert env.plugin.terminal_dot_mode_active(entered)


@pytest.mark.parametrize("command", [".ls", "/term pwd"])
async def test_actual_terminal_operations_extend_mode_and_expiry_boundary_is_exact(
    dot_env, command
):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    env.clock.now += 599
    await dispatch(env, command)
    env.clock.now += 599
    assert env.plugin.terminal_dot_mode_active(entered)
    env.clock.now += 1
    await dispatch(env, ".ls")
    assert not env.plugin.terminal_dot_mode_active(entered)
    assert env.plugin.terminal.execute.await_count == 1
    assert len(env.model_calls) == 1


async def test_repeated_enter_and_rejected_commands_do_not_extend_mode(dot_env):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    env.clock.now += 300
    await dispatch(env, "/term enter")
    for text in (".", ".echo\x00"):
        await dispatch(env, text)
    env.plugin.get_browser_service = lambda: (_ for _ in ()).throw(
        RenderError("不可用")
    )
    await dispatch(env, ".ls")
    env.clock.now += 300
    assert not env.plugin.terminal_dot_mode_active(entered)
    env.plugin.terminal.execute.assert_not_awaited()


@pytest.mark.parametrize("prefix", [".", "./", ".bot"])
async def test_dot_wake_prefix_conflicts_reject_enter(dot_env, prefix):
    env = dot_env
    env.config["wake_prefix"].append(prefix)
    event = await dispatch(env, "/term enter")
    assert "前缀冲突" in "".join(event.sent) and prefix in "".join(event.sent)
    assert not env.plugin._terminal_dot_modes
    env.plugin.terminal.execute.assert_not_awaited()


@pytest.mark.parametrize("alias", [False, True])
async def test_other_dot_commands_and_aliases_are_not_taken_over(dot_env, alias):
    env = dot_env
    calls = register_dot_command(env, alias=alias)
    event = await dispatch(env, "/term enter")
    assert "前缀冲突" in "".join(event.sent) and ".ls" in "".join(event.sent)
    assert not env.plugin._terminal_dot_modes
    await dispatch(env, ".ls")
    assert len(calls) == 1
    env.plugin.terminal.execute.assert_not_awaited()


@pytest.mark.parametrize("active,enabled", [(False, True), (True, False)])
async def test_inactive_dot_registration_does_not_block_mode(dot_env, active, enabled):
    env = dot_env
    register_dot_command(env, active=active, enabled=enabled)
    entered = await dispatch(env, "/term enter")
    assert env.plugin.terminal_dot_mode_active(entered)
    await dispatch(env, ".ls")
    env.plugin.terminal.execute.assert_awaited_once()


async def test_new_dot_conflict_disables_active_mode_without_hijacking_command(dot_env):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    calls = register_dot_command(env)
    await dispatch(env, ".ls")
    assert len(calls) == 1
    assert not env.plugin.terminal_dot_mode_active(entered)
    env.plugin.terminal.execute.assert_not_awaited()


@pytest.mark.parametrize("action", ["expire", "exit"])
async def test_leaving_mode_does_not_cancel_inflight_terminal_execution(
    dot_env, action
):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    started = asyncio.Event()
    release = asyncio.Event()
    execute = env.plugin.terminal.execute.side_effect

    async def running(*args):
        started.set()
        await release.wait()
        return execute(*args)

    env.plugin.terminal.execute.side_effect = running
    task = asyncio.create_task(dispatch(env, ".ls"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        if action == "expire":
            env.clock.now += 600
        else:
            await dispatch(env, "/term exit")
        assert not env.plugin.terminal_dot_mode_active(entered)
        assert not task.done()
    finally:
        release.set()
        completed = await asyncio.wait_for(task, 2)
    assert completed.sent_chains and not task.cancelled()
    document = env.plugin.picture_renderer.render.call_args.args[0]
    assert "点号模式" not in document.footer.details


async def test_real_shell_directory_and_dot_exit_preserve_mode(
    dot_env, monkeypatch, tmp_path
):
    env = dot_env
    monkeypatch.setattr(
        env.plugin.terminal,
        "execute",
        TerminalRunner.execute.__get__(env.plugin.terminal),
    )
    child = tmp_path / "child"
    child.mkdir()
    await dispatch(env, "/term enter")
    await dispatch(env, ".cd child")
    await dispatch(env, "/term pwd")
    document = env.plugin.picture_renderer.render.call_args.args[0]
    assert str(child) in document.footer.directory
    exited = await dispatch(env, ".exit")
    assert env.plugin.terminal_dot_mode_active(exited)
    await dispatch(env, "/term exit")
    await dispatch(env, "/term pwd")
    key = (exited.unified_msg_origin, exited.get_sender_id())
    assert env.plugin.terminal.sessions[key].cwd == child
    await env.plugin.terminal.close()


async def test_terminate_and_new_instance_clear_mode(dot_env):
    env = dot_env
    entered = await dispatch(env, "/term enter")
    new_plugin = env.main.Main(env.context, env.plugin_config)
    assert not new_plugin.terminal_dot_mode_active(entered)
    await env.plugin.terminate()
    assert not env.plugin.ready and not env.plugin._terminal_dot_modes
    assert not env.plugin.matches_terminal_dot(env.event(".ls"))


def test_dot_mode_marker_is_in_footer_only():
    from pathlib import Path

    directory = (
        Path("C:/AstrBot/data/workspace")
        if os.name == "nt"
        else Path("/opt/astrbot/workspace")
    )
    result = TerminalResult("ls", directory, directory, "sh", "example", 0, 0.25)
    html = build_html(result.document(5, dot_mode=True))
    assert "点号模式" in html and "<header>" not in html
    assert "点号模式" not in build_html(result.document(5))
