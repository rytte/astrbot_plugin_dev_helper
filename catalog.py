"""Read command and tool metadata without executing filters or tools."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from functools import partial

from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from .display import redact


@dataclass
class Entry:
    """One independently registered command or tool."""

    kind: str
    name: str
    plugin: str
    source: str
    status: str
    details: str

    def render(self) -> str:
        """Render metadata with explicit ownership and global state.

        Returns:
            A sanitized catalog entry.
        """
        return redact(
            f"[{self.kind}] {self.name}\n"
            f"来源：{self.source} / {self.plugin}；{self.status}\n{self.details}",
        )


def command_entries() -> list[Entry]:
    """List registered commands, including groups, aliases and parent policy.

    Returns:
        Entries sorted by plugin and canonical command name.
    """
    handlers = list(star_handlers_registry)
    groups = [
        (handler, command_filter)
        for handler in handlers
        for command_filter in handler.event_filters
        if isinstance(command_filter, CommandGroupFilter)
    ]
    entries = []
    for handler in handlers:
        if handler.event_type != EventType.AdapterMessageEvent:
            continue
        owner = star_map.get(handler.handler_module_path)
        for command_filter in handler.event_filters:
            if not isinstance(command_filter, (CommandFilter, CommandGroupFilter)):
                continue
            names = command_filter.get_complete_command_names()
            canonical = names[0]
            ancestors = [
                (parent, group)
                for parent, group in groups
                if parent.handler_module_path == handler.handler_module_path
                and any(
                    canonical.startswith(name + " ")
                    for name in group.get_complete_command_names()
                )
            ]
            filters = [*handler.event_filters, *command_filter.custom_filter_list]
            for parent, group in ancestors:
                filters.extend(parent.event_filters)
                filters.extend(group.custom_filter_list)
            admin = any(
                isinstance(item, PermissionTypeFilter)
                and item.permission_type == PermissionType.ADMIN
                for item in filters
            )
            enabled = bool(owner and owner.activated and handler.enabled)
            enabled = enabled and all(parent.enabled for parent, _ in ancestors)
            usage = (
                command_filter.print_types()
                if isinstance(command_filter, CommandFilter)
                else "指令组"
            )
            description = handler.desc.splitlines()[0] if handler.desc else "无说明"
            details = [
                f"权限：{'管理员' if admin else '成员'}（其他过滤条件仍生效）",
                f"用法：{canonical}" + (f" {usage}" if usage else ""),
                f"说明：{description}",
            ]
            if len(names) > 1:
                details.append("别名：" + "、".join(sorted(set(names[1:]))))
            entries.append(
                Entry(
                    kind="命令组"
                    if isinstance(command_filter, CommandGroupFilter)
                    else "命令",
                    name=canonical,
                    plugin=owner.name
                    if owner and owner.name
                    else handler.handler_module_path,
                    source="内置" if owner and owner.reserved else "插件",
                    status="全局启用" if enabled else "已停用或来源未加载",
                    details="\n".join(details),
                )
            )
    return sorted(entries, key=lambda item: (item.plugin, item.name))


def tool_entries(manager) -> list[Entry]:
    """List registered tools and Agent children without invoking them.

    Args:
        manager: AstrBot's current FunctionToolManager.

    Returns:
        Entries preserving distinct registrations and all direct Agent parents.
    """
    entries = []
    builtins = manager.iter_builtin_tools()
    builtin_ids = {id(tool) for tool in builtins}
    tools = {}
    parents = defaultdict(set)
    unresolved = defaultdict(set)
    pending = [*manager.func_list, *builtins]
    for tool in pending:
        if id(tool) in tools:
            continue
        tools[id(tool)] = tool
        if isinstance(tool, HandoffTool):
            for child in tool.agent.tools or []:
                if isinstance(child, str):
                    reference = child
                    child = manager.get_func(reference)
                    if child is None:
                        unresolved[id(tool)].add(reference)
                        continue
                if not isinstance(child, FunctionTool):
                    raise ValueError(
                        f"Agent {tool.agent.name} 含有无效的工具注册信息。"
                    )
                parents[id(child)].add(f"{tool.agent.name}（{tool.name}）")
                pending.append(child)

    for tool in tools.values():
        owner = None
        if id(tool) in builtin_ids:
            source, plugin = "内置", "AstrBot"
        elif isinstance(tool, MCPTool):
            source, plugin = "MCP", tool.mcp_server_name
        else:
            module = tool.handler_module_path
            # register_agent stores ownership on its callback rather than on
            # HandoffTool.handler_module_path in the current AstrBot API.
            if not module and tool.handler is not None:
                callback = tool.handler
                while isinstance(callback, partial):
                    callback = callback.func
                module = getattr(callback, "__module__", None)
            owner = star_map.get(module)
            if module and owner is None:
                matches = [
                    metadata
                    for metadata in star_map.values()
                    if metadata.module_path
                    and "." in metadata.module_path
                    and module.startswith(metadata.module_path.rsplit(".", 1)[0] + ".")
                ]
                if matches:
                    owner = max(matches, key=lambda metadata: len(metadata.module_path))
            source = "内置" if owner and owner.reserved else "插件"
            plugin = owner.name if owner and owner.name else (module or "未识别来源")
        active = tool.active and (owner is None or owner.activated)
        description = tool.description or "无说明"
        parameters = json.dumps(tool.parameters, ensure_ascii=False)
        details = []
        if isinstance(tool, HandoffTool):
            details.append(f"Agent：{tool.agent.name}")
            if tool.agent.tools is None:
                details.append("Agent 工具范围：按请求决定（未指定工具列表）")
            if unresolved[id(tool)]:
                details.append(
                    "工具引用信息不可用（按请求决定）："
                    + "、".join(sorted(unresolved[id(tool)]))
                )
        if parents[id(tool)]:
            details.append("所属 Agent 入口：" + "、".join(sorted(parents[id(tool)])))
        details.extend((f"说明：{description}", f"参数：{parameters}"))
        entries.append(
            Entry(
                kind="Agent 入口" if isinstance(tool, HandoffTool) else "工具",
                name=tool.name,
                plugin=plugin,
                source=source,
                status=("全局启用；会话可用性按请求决定" if active else "已停用"),
                details="\n".join(details),
            )
        )
    return sorted(entries, key=lambda item: (item.source, item.plugin, item.name))


def command_conflicts(module: str) -> list[str]:
    """Find overlapping root command names, including configured aliases.

    Args:
        module: Owning module of this plugin.

    Returns:
        Sorted conflict descriptions, without changing any registry state.
    """
    registrations = [
        (handler, command_filter.get_complete_command_names())
        for handler in star_handlers_registry
        for command_filter in handler.event_filters
        if isinstance(command_filter, (CommandFilter, CommandGroupFilter))
    ]
    own_roots = {
        name.split()[0]
        for handler, names in registrations
        if handler.handler_module_path == module
        for name in names
    }
    conflicts = set()
    for handler, names in registrations:
        if handler.handler_module_path == module:
            continue
        for name in names:
            if name.split()[0] in own_roots:
                conflicts.add(f"{name}（{handler.handler_module_path}）")
    return sorted(conflicts)
