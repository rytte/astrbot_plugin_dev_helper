"""Verify targeted reloads without touching an installed AstrBot instance."""

import asyncio
import copy
import importlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata
from astrbot.core.star.star_manager import PluginManager
from astrbot.dashboard.services.plugin_service import PluginService
from astrbot_plugin_dev_helper.plugin_reload import clear_plugin_bytecode


@pytest.fixture
def reload_env(env, monkeypatch, tmp_path):
    target = StarMetadata(
        name="demo_plugin",
        module_path="data.plugins.demo_directory.main",
        root_dir_name="demo_directory",
        star_cls=object(),
        activated=True,
    )
    env.owners[target.module_path] = target
    env.context.get_registered_star = lambda name: next(
        (item for item in env.owners.values() if item.name == name), None
    )
    actions = []
    (tmp_path / "demo_directory").mkdir()
    manager = SimpleNamespace(
        context=env.context,
        _pm_lock=asyncio.Lock(),
        reload=AsyncMock(side_effect=AssertionError("Unsafe reload was called")),
        turn_on_plugin=AsyncMock(),
        turn_off_plugin=AsyncMock(),
        reload_failed_plugin=AsyncMock(),
        failed_plugin_dict={"broken_directory": {}},
        plugin_store_path=str(tmp_path),
        reserved_plugin_path=str(tmp_path),
    )

    async def terminate(plugin):
        assert manager._pm_lock.locked()
        actions.append("terminate")

    async def unbind(name, module_path):
        assert manager._pm_lock.locked()
        actions.append("unbind")
        del env.owners[module_path]

    async def load(*, specified_module_path):
        assert manager._pm_lock.locked()
        assert specified_module_path == target.module_path
        actions.append("load")
        reloaded = copy.copy(target)
        reloaded.star_cls = object()
        env.owners[specified_module_path] = reloaded
        return True, None

    async def sync_skills():
        assert not manager._pm_lock.locked()
        actions.append("sync")

    manager._terminate_plugin = AsyncMock(side_effect=terminate)
    manager._unbind_plugin = AsyncMock(side_effect=unbind)
    manager.load = AsyncMock(side_effect=load)
    sync = AsyncMock(side_effect=sync_skills)
    monkeypatch.setattr(PluginService, "sync_skills_after_plugin_change", sync)
    env.context._star_manager = manager
    env.reload_manager = manager
    env.reload_target = target
    env.reload_actions = actions
    env.reload_sync = sync
    return env


@pytest.mark.parametrize("group", [None, "group"])
@pytest.mark.parametrize("name", ["demo_plugin", "demo_directory"])
async def test_reload_runs_without_confirmation_in_private_and_group(
    reload_env, group, name
):
    env = reload_env
    event = env.event(f"/plugin reload {name}", group=group)
    await env.scheduler.execute(event)
    assert event.sent == ["正在重载插件 demo_plugin。", "插件 demo_plugin 已重载。"]
    assert event.is_stopped()
    assert env.reload_actions == ["terminate", "unbind", "load", "sync"]
    env.reload_manager._terminate_plugin.assert_awaited_once_with(env.reload_target)
    env.reload_manager._unbind_plugin.assert_awaited_once_with(
        "demo_plugin", "data.plugins.demo_directory.main"
    )
    env.reload_manager.reload.assert_not_awaited()
    env.reload_manager.turn_on_plugin.assert_not_awaited()
    env.reload_manager.turn_off_plugin.assert_not_awaited()
    assert not env.model_calls


async def test_reload_coexists_with_builtin_plugin_group(reload_env):
    env = reload_env

    def builtin_group(self, event):
        raise AssertionError("A sibling command consumed the reload input")

    group = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "builtin_plugin_group",
        "plugin",
        "builtin.main",
        builtin_group,
        [],
    )
    group.event_filters = [CommandGroupFilter("plugin")]
    env.registry.append(group)
    sibling = StarHandlerMetadata(
        EventType.AdapterMessageEvent,
        "builtin_plugin_on",
        "plugin_on",
        "builtin.main",
        builtin_group,
        [],
    )
    sibling.event_filters = [CommandFilter("on", None, sibling, ["plugin"])]
    env.registry.append(sibling)
    env.owners["builtin.main"] = StarMetadata(name="builtin", activated=True)
    await env.plugin.initialize()
    event = env.event("/plugin reload demo_plugin")
    await env.scheduler.execute(event)
    assert "已重载" in "".join(event.sent)
    assert env.reload_actions == ["terminate", "unbind", "load", "sync"]


async def test_reload_cannot_bypass_administrator_permissions(reload_env):
    env = reload_env
    event = env.event("/plugin reload demo_plugin", user="member", admin=False)
    await env.scheduler.execute(event)
    assert "权限" in "".join(event.sent)
    event = env.event(user="member", admin=False)
    await env.invoke("plugin_reload", event, "demo_plugin")
    assert "仅限" in "".join(event.sent)
    event = env.event()
    event.set_extra("_api_key_allow_admin_role", False)
    await env.invoke("plugin_reload", event, "demo_plugin")
    assert "仅限" in "".join(event.sent)
    assert not env.reload_actions


@pytest.mark.parametrize(
    "arguments",
    ["", "--all", "demo_plugin confirm", "demo_plugin --all", "demo_plugin another"],
)
async def test_reload_rejects_missing_target_and_extra_arguments(reload_env, arguments):
    event = reload_env.event(f"/plugin reload {arguments}".strip())
    await reload_env.scheduler.execute(event)
    assert "用法" in "".join(event.sent)
    assert event.is_stopped()
    assert not reload_env.reload_actions
    assert not reload_env.model_calls


@pytest.mark.parametrize("name", ["unknown", "broken_directory", "../demo_directory"])
async def test_reload_never_falls_back_to_all_or_failed_plugin_recovery(
    reload_env, name
):
    event = reload_env.event()
    await reload_env.invoke("plugin_reload", event, name)
    assert "未找到已加载插件" in "".join(event.sent)
    assert not reload_env.reload_actions
    reload_env.reload_manager.reload.assert_not_awaited()
    reload_env.reload_manager.reload_failed_plugin.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("activated", False, "已停用"),
        ("module_path", None, "不完整"),
        ("root_dir_name", None, "不完整"),
        ("star_cls", None, "不完整"),
    ],
)
async def test_reload_rejects_disabled_or_incomplete_plugin(
    reload_env, field, value, message
):
    setattr(reload_env.reload_target, field, value)
    event = reload_env.event()
    await reload_env.invoke("plugin_reload", event, "demo_plugin")
    assert message in "".join(event.sent)
    assert not reload_env.reload_actions
    reload_env.reload_manager.turn_on_plugin.assert_not_awaited()


async def test_reload_reports_missing_manager(reload_env):
    reload_env.context._star_manager = None
    event = reload_env.event()
    await reload_env.invoke("plugin_reload", event, "demo_plugin")
    assert "插件管理器不可用" in "".join(event.sent)
    assert not reload_env.reload_actions


async def test_reload_is_forbidden_in_demo_mode(reload_env, monkeypatch):
    service_module = importlib.import_module(
        "astrbot.dashboard.services.plugin_service"
    )
    monkeypatch.setattr(service_module, "DEMO_MODE", True)
    event = reload_env.event()
    await reload_env.invoke("plugin_reload", event, "demo_plugin")
    assert "demo mode" in "".join(event.sent)
    assert not reload_env.reload_actions


@pytest.mark.parametrize(
    "change",
    [
        "remove",
        "replace",
        "disable",
        "instance",
        "module_path",
        "root_dir_name",
        "reserved",
    ],
)
async def test_reload_revalidates_target_after_waiting_for_manager_lock(
    reload_env, change
):
    env = reload_env
    manager = env.reload_manager
    await manager._pm_lock.acquire()
    event = env.event()
    task = asyncio.create_task(env.invoke("plugin_reload", event, "demo_plugin"))
    try:
        await asyncio.sleep(0)
        assert event.sent == ["正在重载插件 demo_plugin。"]
        assert not task.done()
        target = env.reload_target
        if change == "remove":
            del env.owners[target.module_path]
        elif change == "replace":
            env.owners[target.module_path] = copy.copy(target)
        elif change == "disable":
            target.activated = False
        elif change == "instance":
            target.star_cls = object()
        elif change == "module_path":
            target.module_path = "another.main"
        elif change == "root_dir_name":
            target.root_dir_name = "another_directory"
        else:
            target.reserved = True
    finally:
        manager._pm_lock.release()
        await task
    assert "状态已变化" in "".join(event.sent)
    assert not env.reload_actions
    manager.reload.assert_not_awaited()


@pytest.mark.parametrize("operation", ["_terminate_plugin", "_unbind_plugin", "load"])
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_reload_reports_lifecycle_errors_without_claiming_success(
    reload_env, operation, error_type
):
    env = reload_env
    getattr(env.reload_manager, operation).side_effect = error_type("lifecycle failed")
    event = env.event()
    await env.invoke("plugin_reload", event, "demo_plugin")
    assert "失败：lifecycle failed" in "".join(event.sent)
    assert "WebUI" in "".join(event.sent)
    assert "已重载" not in "".join(event.sent)
    assert not env.reload_manager._pm_lock.locked()
    env.reload_sync.assert_not_awaited()
    if operation == "_terminate_plugin":
        env.reload_manager._unbind_plugin.assert_not_awaited()
        env.reload_manager.load.assert_not_awaited()


@pytest.mark.parametrize("mode", ["failure", "missing", "old_instance"])
async def test_reload_requires_successful_registration_of_a_new_instance(
    reload_env, mode
):
    env = reload_env
    manager = env.reload_manager
    if mode == "failure":
        manager.load.side_effect = None
        manager.load.return_value = (False, "initialize failed")
    else:
        original_load = manager.load.side_effect

        async def load(*, specified_module_path):
            result = await original_load(specified_module_path=specified_module_path)
            if mode == "missing":
                del env.owners[specified_module_path]
            else:
                env.owners[specified_module_path].star_cls = env.reload_target.star_cls
            return result

        manager.load.side_effect = load
    event = env.event()
    await env.invoke("plugin_reload", event, "demo_plugin")
    assert "失败" in "".join(event.sent)
    assert "已重载" not in "".join(event.sent)
    env.reload_sync.assert_not_awaited()


@pytest.mark.parametrize("name", ["plugin", "plugin reload"])
async def test_reload_detects_conflicting_command_registrations(env, name):
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


@pytest.fixture
def live_reload_env(env, monkeypatch, tmp_path):
    manager_module = importlib.import_module("astrbot.core.star.star_manager")
    stars = list(env.owners.values())
    for module_name in (
        "astrbot.core.star.star",
        "astrbot.core.star.base",
        "astrbot.core.star.context",
        "astrbot.core.star.star_manager",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "star_map", env.owners)
        monkeypatch.setattr(module, "star_registry", stars)
    monkeypatch.setattr(manager_module, "star_handlers_registry", env.registry)
    monkeypatch.setattr(manager_module.llm_tools, "func_list", env.manager.func_list)
    monkeypatch.setattr(
        manager_module,
        "sp",
        SimpleNamespace(
            global_get=AsyncMock(side_effect=lambda key, default=None: default)
        ),
    )
    monkeypatch.setattr(manager_module, "sync_command_configs", AsyncMock())
    sync = AsyncMock()
    monkeypatch.setattr(PluginService, "sync_skills_after_plugin_change", sync)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    plugin_root = tmp_path / "data" / "plugins"
    plugin_root.mkdir(parents=True)
    for name, path in (("data", plugin_root.parent), ("data.plugins", plugin_root)):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        monkeypatch.setitem(sys.modules, name, package)
    manager = PluginManager.__new__(PluginManager)
    manager.context = env.context
    manager.config = env.config
    manager.plugin_store_path = str(plugin_root)
    manager.plugin_config_path = str(tmp_path / "config")
    Path(manager.plugin_config_path).mkdir()
    manager.reserved_plugin_path = str(tmp_path / "builtin")
    manager.conf_schema_fname = "_conf_schema.json"
    manager.logo_fname = "logo.png"
    manager.failed_plugin_dict = {}
    manager.failed_plugin_info = ""
    manager._pm_lock = asyncio.Lock()
    modules = []
    manager._get_plugin_modules = lambda: modules
    env.context._star_manager = manager
    env.context.get_registered_star = lambda name: next(
        (item for item in stars if item.name == name), None
    )
    env.live_manager = manager
    env.live_modules = modules
    env.live_root = plugin_root
    env.live_stars = stars
    env.live_source_root = Path(env.main.__file__).resolve().parent
    env.reload_sync = sync
    yield env
    for module_name in list(sys.modules):
        if module_name.startswith(
            ("data.plugins.reload_probe", "data.plugins.astrbot_plugin_dev_helper")
        ):
            del sys.modules[module_name]


@pytest.mark.parametrize("desktop", [False, True])
async def test_reload_reimports_main_helpers_commands_and_tools(
    live_reload_env, monkeypatch, desktop
):
    env = live_reload_env
    monkeypatch.setenv("ASTRBOT_DESKTOP_CLIENT", "1" if desktop else "0")
    monkeypatch.setenv("ASTRBOT_DESKTOP_MANAGED", "1" if desktop else "0")
    directory = env.live_root / "reload_probe"
    directory.mkdir()
    (directory / "metadata.yaml").write_text(
        "name: reload_probe\nauthor: Probe\ndesc: Reload probe\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    helper = directory / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    (directory / "main.py").write_text(
        '''from astrbot.api.star import Star
from astrbot.api.event import filter
from .helper import VALUE
class Main(Star):
    def __init__(self, context):
        self.context = context
        self.marker = VALUE
    async def initialize(self):
        self.initialized = True
    async def terminate(self):
        self.terminated = True
    @filter.command("probe_command")
    async def probe(self, event):
        event.set_result(event.plain_result(str(VALUE)))
    @filter.llm_tool(name="probe_tool")
    async def tool(self, event):
        """Return the current probe revision."""
        return str(VALUE)
''',
        encoding="utf-8",
    )
    env.live_modules.append({"pname": "reload_probe", "module": "main"})
    success, error = await env.live_manager.load(specified_dir_name="reload_probe")
    assert success, error
    original = env.context.get_registered_star("reload_probe")
    previous_instance = original.star_cls
    old_main = sys.modules[original.module_path]
    old_helper = sys.modules["data.plugins.reload_probe.helper"]
    assert await asyncio.to_thread(Path(old_helper.__cached__).exists)
    original_stat = await asyncio.to_thread(helper.stat)
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    await asyncio.to_thread(
        os.utime, helper, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns)
    )
    event = env.event("/plugin reload reload_probe")
    await env.scheduler.execute(event)
    assert "已重载" in "".join(event.sent)
    reloaded = env.context.get_registered_star("reload_probe")
    assert previous_instance.terminated
    assert reloaded.star_cls is not previous_instance
    assert reloaded.star_cls.marker == 2 and reloaded.star_cls.initialized
    assert sys.modules[reloaded.module_path] is not old_main
    assert sys.modules["data.plugins.reload_probe.helper"] is not old_helper
    commands = [
        handler
        for handler in env.registry
        if handler.handler_module_path == reloaded.module_path
        and handler.handler_name == "probe"
    ]
    assert len(commands) == 1
    assert commands[0].handler.args == (reloaded.star_cls,)
    tools = [tool for tool in env.manager.func_list if tool.name == "probe_tool"]
    assert len(tools) == 1
    assert await tools[0].handler(env.event()) == "2"
    env.reload_sync.assert_awaited_once_with()
    assert not env.model_calls


@pytest.mark.parametrize("invalid_config", [False, True])
async def test_dev_helper_reloads_its_own_module_and_resets_runtime_state(
    live_reload_env,
    invalid_config,
):
    env = live_reload_env
    source_root = env.live_source_root
    directory = env.live_root / "astrbot_plugin_dev_helper"
    directory.mkdir()
    for source_path in [
        *source_root.glob("*.py"),
        source_root / "metadata.yaml",
        source_root / "_conf_schema.json",
    ]:
        shutil.copy2(source_path, directory / source_path.name)
    main_path = directory / "main.py"
    main_source = main_path.read_text(encoding="utf-8")
    main_path.write_text(main_source + "\nRELOAD_REVISION = 1\n", encoding="utf-8")
    env.owners.clear()
    env.registry.clear()
    env.live_stars.clear()
    env.live_modules.append({"pname": directory.name, "module": "main"})
    success, error = await env.live_manager.load(specified_dir_name=directory.name)
    assert success, error
    original = env.context.get_registered_star(directory.name)
    previous_instance = original.star_cls
    old_main = sys.modules[original.module_path]
    old_catalog = sys.modules["data.plugins.astrbot_plugin_dev_helper.catalog"]
    previous_instance._terminal_dot_modes[("session", "admin")] = 42
    previous_instance._restart_confirmations[("session", "admin")] = 42
    previous_instance._plugin_removals[("session", "admin")] = object()
    config_path = (
        Path(env.live_manager.plugin_config_path) / f"{directory.name}_config.json"
    )
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    config["logs_default_count"] = 0 if invalid_config else 7
    config_path.write_text(json.dumps(config), encoding="utf-8")
    main_path.write_text(main_source + "\nRELOAD_REVISION = 2\n", encoding="utf-8")
    event = env.event(f"/plugin reload {directory.name}")
    await env.scheduler.execute(event)
    if invalid_config:
        assert "失败" in "".join(event.sent) and "WebUI" in "".join(event.sent)
        assert "已重载" not in "".join(event.sent)
        assert not previous_instance.ready and previous_instance.terminal.closed
        assert env.context.get_registered_star(directory.name) is None
        assert event.is_stopped() and not env.model_calls
        env.reload_sync.assert_not_awaited()
        return
    assert event.sent == [
        f"正在重载插件 {directory.name}。",
        f"插件 {directory.name} 已重载。",
    ]
    reloaded = env.context.get_registered_star(directory.name)
    assert reloaded.star_cls is not previous_instance
    assert not previous_instance.ready and previous_instance.terminal.closed
    assert reloaded.star_cls.ready and not reloaded.star_cls.terminal.closed
    assert reloaded.star_cls.logs_default_count == 7
    assert not reloaded.star_cls._terminal_dot_modes
    assert not reloaded.star_cls._restart_confirmations
    assert not reloaded.star_cls._plugin_removals
    assert sys.modules[reloaded.module_path] is not old_main
    assert sys.modules[reloaded.module_path].RELOAD_REVISION == 2
    assert (
        sys.modules["data.plugins.astrbot_plugin_dev_helper.catalog"] is not old_catalog
    )
    commands = [
        handler for handler in env.registry if handler.handler_name == "plugin_reload"
    ]
    assert len(commands) == 1 and commands[0].handler.args == (reloaded.star_cls,)
    assert event.is_stopped()
    env.reload_sync.assert_awaited_once_with()
    assert not env.model_calls


async def test_bytecode_cleanup_failure_does_not_terminate_plugin(
    reload_env, monkeypatch
):
    env = reload_env

    def denied(*args):
        raise PermissionError("cache permission denied")

    monkeypatch.setattr(env.main, "clear_plugin_bytecode", denied)
    event = env.event()
    await env.invoke("plugin_reload", event, "demo_plugin")
    assert "失败：cache permission denied" in "".join(event.sent)
    assert not env.reload_actions
    assert not env.reload_manager._pm_lock.locked()


def test_bytecode_cleanup_only_removes_source_backed_plugin_caches(tmp_path):
    directory = tmp_path / "plugin"
    cache_dir = directory / "__pycache__"
    cache_dir.mkdir(parents=True)
    (directory / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    cache = cache_dir / "main.cpython-312.pyc"
    cache.write_bytes(b"cached code")
    retained = cache_dir / "bytecode_only.cpython-312.pyc"
    retained.write_bytes(b"bytecode only")
    data = directory / "data.json"
    data.write_text("{}", encoding="utf-8")
    sibling = tmp_path / "another_plugin.pyc"
    sibling.write_bytes(b"unrelated cache")
    clear_plugin_bytecode(str(tmp_path), directory.name)
    assert not cache.exists()
    assert retained.exists() and data.exists() and sibling.exists()


@pytest.mark.parametrize("name", [".", "../outside"])
def test_bytecode_cleanup_rejects_root_or_external_plugin_directory(tmp_path, name):
    store = tmp_path / "plugins"
    store.mkdir()
    (tmp_path / "outside").mkdir()
    with pytest.raises(RuntimeError, match="直接子目录"):
        clear_plugin_bytecode(str(store), name)


def test_bytecode_cleanup_rejects_external_cache_before_deleting_any_file(
    tmp_path, monkeypatch
):
    directory = tmp_path / "plugin"
    cache_dir = directory / "__pycache__"
    cache_dir.mkdir(parents=True)
    (directory / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    cache = cache_dir / "main.cpython-312.pyc"
    cache.write_bytes(b"cached code")
    external = tmp_path / "external.pyc"
    external.write_bytes(b"external cache")
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == cache:
            return external
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(RuntimeError, match="缓存指向插件目录之外"):
        clear_plugin_bytecode(str(tmp_path), directory.name)
    assert cache.exists() and external.exists()
