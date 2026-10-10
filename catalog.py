"""Detect command conflicts without executing filters or handlers."""

from __future__ import annotations

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry


def terminal_dot_conflicts(module: str, wake_prefixes: list[str]) -> list[str]:
    conflicts = {
        f"唤醒前缀 {prefix!r}" for prefix in wake_prefixes if prefix.startswith(".")
    }
    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent
    ):
        if handler.handler_module_path == module:
            continue
        for command_filter in handler.event_filters:
            if isinstance(command_filter, CommandFilter | CommandGroupFilter):
                conflicts.update(
                    f"{name}（{handler.handler_module_path}）"
                    for name in command_filter.get_complete_command_names()
                    if name.startswith(".")
                )
    return sorted(conflicts)


def command_conflicts(module: str) -> list[str]:
    """Find commands that can consume the same input, including aliases.

    Args:
        module: Owning module of this plugin.

    Returns:
        Sorted conflict descriptions, without changing any registry state.
    """
    registrations = [
        (handler, command_filter, command_filter.get_complete_command_names())
        for handler in star_handlers_registry
        for command_filter in handler.event_filters
        if isinstance(command_filter, CommandFilter | CommandGroupFilter)
    ]
    own_commands = [
        (command_filter, name)
        for handler, command_filter, names in registrations
        if handler.handler_module_path == module
        for name in names
    ]
    conflicts = set()
    for handler, command_filter, names in registrations:
        if handler.handler_module_path == module:
            continue
        for name in names:
            if any(
                own_name == name
                or (
                    isinstance(own_filter, CommandFilter)
                    and name.startswith(own_name + " ")
                )
                or (
                    isinstance(command_filter, CommandFilter)
                    and own_name.startswith(name + " ")
                )
                for own_filter, own_name in own_commands
            ):
                conflicts.add(f"{name}（{handler.handler_module_path}）")
    return sorted(conflicts)
