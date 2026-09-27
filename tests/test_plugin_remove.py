"""Exercise plugin removal without deleting any real plugin files."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.dashboard.services.plugin_service import PluginService


@pytest.fixture
def remove_env(env, monkeypatch):
    plugin = StarMetadata(
        name="demo_plugin",
        module_path="demo.main",
        root_dir_name="demo_directory",
        activated=True,
    )
    current = [plugin]
    env.context.get_registered_star = (
        lambda name: current[0] if current[0] and name == current[0].name else None
    )
    env.context.get_all_stars = lambda: [current[0]] if current[0] else []
    manager = SimpleNamespace(context=env.context, failed_plugin_dict={})
    env.context._star_manager = manager
    uninstall = AsyncMock(return_value=(None, "卸载成功"))
    uninstall_failed = AsyncMock(return_value=(None, "卸载成功"))
    monkeypatch.setattr(PluginService, "uninstall_plugin", uninstall)
    monkeypatch.setattr(PluginService, "uninstall_failed_plugin", uninstall_failed)
    return env, current, manager, uninstall, uninstall_failed


async def test_webui_service_uninstall_path_accepts_plugin_manager_without_dashboard():
    plugin = StarMetadata(name="demo_plugin", root_dir_name="demo_directory")
    context = SimpleNamespace(
        get_registered_star=lambda name: plugin if name == plugin.name else None,
        get_all_stars=lambda: [plugin],
    )
    manager = SimpleNamespace(context=context, uninstall_plugin=AsyncMock())
    service = PluginService(None, manager)
    service.remove_plugin_install_source = AsyncMock()
    service.sync_skills_after_plugin_change = AsyncMock()

    await service.uninstall_plugin(
        {"name": "demo_plugin", "delete_config": True, "delete_data": True}
    )

    manager.uninstall_plugin.assert_awaited_once_with(
        "demo_plugin", delete_config=True, delete_data=True
    )
    service.remove_plugin_install_source.assert_awaited_once_with("demo_directory")
    service.sync_skills_after_plugin_change.assert_awaited_once_with()


async def test_remove_shares_builtin_plugin_group_and_uses_webui_service(remove_env):
    env, _, _, uninstall, _ = remove_env

    def builtin_group(self, event):
        raise AssertionError("A command group must not consume a child command")

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "builtin_plugin_group",
        "plugin",
        "builtin.main",
        builtin_group,
        [],
    )
    handler.event_filters = [CommandGroupFilter("plugin")]
    env.registry.append(handler)
    sibling = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "builtin_plugin_off",
        "plugin_off",
        "builtin.main",
        builtin_group,
        [],
    )
    sibling.event_filters = [CommandFilter("off", None, sibling, ["plugin"])]
    env.registry.append(sibling)
    env.owners["builtin.main"] = StarMetadata(name="builtin", activated=True)
    await env.plugin.initialize()

    request = env.event("/plugin remove demo_directory")
    await env.scheduler.execute(request)
    assert "请在 60 秒内发送 /plugin remove demo_plugin confirm" in "".join(
        request.sent
    )
    assert "配置" in "".join(request.sent) and "数据" in "".join(request.sent)
    uninstall.assert_not_awaited()

    confirmation = env.event("/plugin remove demo_plugin confirm")
    await env.scheduler.execute(confirmation)
    uninstall.assert_awaited_once_with(
        {"name": "demo_plugin", "delete_config": True, "delete_data": True}
    )
    assert "已卸载" in "".join(confirmation.sent)
    assert confirmation.is_stopped()


async def test_remove_always_deletes_config_and_data(remove_env):
    env, _, _, uninstall, _ = remove_env
    await env.invoke("plugin_remove", env.event(), "demo_plugin")
    await env.invoke("plugin_remove", env.event(), "demo_plugin confirm")
    uninstall.assert_awaited_once_with(
        {"name": "demo_plugin", "delete_config": True, "delete_data": True}
    )


async def test_remove_failed_plugin_uses_webui_failed_plugin_service(remove_env):
    env, current, manager, uninstall, uninstall_failed = remove_env
    current[0] = None
    manager.failed_plugin_dict["broken_directory"] = object()

    request = env.event()
    await env.invoke("plugin_remove", request, "broken_directory")
    assert "broken_directory confirm" in "".join(request.sent)
    await env.invoke("plugin_remove", env.event(), "broken_directory confirm")
    uninstall_failed.assert_awaited_once_with(
        {
            "dir_name": "broken_directory",
            "delete_config": True,
            "delete_data": True,
        }
    )
    uninstall.assert_not_awaited()


async def test_remove_requires_admin_private_confirmation_and_same_plugin(remove_env):
    env, current, _, uninstall, _ = remove_env
    member = env.event(user="member", admin=False)
    pipeline_member = env.event(
        "/plugin remove demo_plugin", user="member", admin=False
    )
    await env.scheduler.execute(pipeline_member)
    assert "权限" in "".join(pipeline_member.sent)
    await env.invoke("plugin_remove", member, "demo_plugin")
    assert "仅限" in "".join(member.sent)

    group = env.event(group="group")
    await env.invoke("plugin_remove", group, "demo_plugin")
    assert "私聊" in "".join(group.sent)

    api_role = env.event()
    api_role.set_extra("_api_key_allow_admin_role", False)
    await env.invoke("plugin_remove", api_role, "demo_plugin")
    assert "仅限" in "".join(api_role.sent)

    await env.invoke("plugin_remove", env.event(), "demo_plugin")
    other_session = env.event(platform="other_platform")
    await env.invoke("plugin_remove", other_session, "demo_plugin confirm")
    assert "已过期" in "".join(other_session.sent)

    current[0] = StarMetadata(
        name="demo_plugin",
        module_path="demo.main",
        root_dir_name="demo_directory",
        activated=True,
    )
    changed = env.event()
    await env.invoke("plugin_remove", changed, "demo_plugin confirm")
    assert "状态已变化" in "".join(changed.sent)
    uninstall.assert_not_awaited()


async def test_remove_confirmation_expires(remove_env):
    env, _, _, uninstall, _ = remove_env
    first = env.event()
    await env.invoke("plugin_remove", first, "demo_plugin")
    key = (first.unified_msg_origin, first.get_sender_id())
    pending = env.plugin._plugin_removals[key]
    env.plugin._plugin_removals[key] = (
        env.main.time.monotonic() - 1,
        *pending[1:],
    )
    expired = env.event()
    await env.invoke("plugin_remove", expired, "demo_plugin confirm")
    assert "已过期" in "".join(expired.sent)
    uninstall.assert_not_awaited()


async def test_remove_rejects_reserved_missing_and_invalid_options(remove_env):
    env, current, _, uninstall, _ = remove_env
    current[0].reserved = True
    reserved = env.event()
    await env.invoke("plugin_remove", reserved, "demo_plugin")
    assert "保留插件" in "".join(reserved.sent)

    current[0] = None
    missing = env.event()
    await env.invoke("plugin_remove", missing, "unknown")
    assert "未找到" in "".join(missing.sent)

    for arguments in (
        "unknown --force",
        "unknown --delete-config",
        "unknown --delete-data",
    ):
        invalid = env.event()
        await env.invoke("plugin_remove", invalid, arguments)
        assert "用法" in "".join(invalid.sent)
    uninstall.assert_not_awaited()


@pytest.mark.parametrize("name", ["plugin", "plugin remove"])
async def test_remove_detects_real_command_conflicts(env, name):
    def colliding(self, event):
        raise AssertionError("Must not dispatch")

    handler = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "other_plugin_command",
        "collision",
        "other.main",
        colliding,
        [],
    )
    handler.event_filters = [CommandFilter(name, None, handler)]
    env.registry.append(handler)
    with pytest.raises(RuntimeError, match="命令冲突"):
        await env.plugin.initialize()
