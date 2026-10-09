"""Administrator diagnostics for AstrBot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import dump_messages_with_checkpoints
from astrbot.core.desktop_runtime import is_desktop_managed_backend
from astrbot.core.log import LogQueueHandler
from astrbot.core.process_restart import restart_process
from astrbot.core.star.filter.command import GreedyStr

from .catalog import command_conflicts, command_entries, tool_entries
from .context_usage import ContextUsage, format_round, history_hashes, usage_picture
from .desktop_restart import schedule_desktop_restart
from .display import history_text, positive_number, redact
from .pictures import LocalPictureRenderer, PictureBlock, PictureDocument, RenderError
from .terminal import TerminalRunner, session_workspace

DEV_USAGE = "/dev commands [页码]\n/dev tools [页码]\n/dev plugin <插件标识> [页码]"
LOG_USAGE = "/logs [条目数] [--text]\n/logs warning [条目数] [--text]"
CHATLOG_USAGE = "/chatlog [条目数] [--text]"
CTX_USAGE = "/ctx [轮数] [--text]"
HELP = (
    "\n查询\n"
    + DEV_USAGE
    + "\n\n日志与对话\n"
    + LOG_USAGE
    + "\n"
    + CHATLOG_USAGE
    + "\n"
    + CTX_USAGE
    + "\n默认输出图片，渲染不可用时回退文本；--text 强制文本，须放在参数末尾。"
    + "\n\n管理（管理员私聊）\n/term <命令>\n/restart"
    + "\n/plugin remove <插件名> [--all]"
)
PAGE_SIZE = 20
CONFIRM_SECONDS = 60


def output_arguments(arguments: str) -> tuple[list[str], bool]:
    args = arguments.split()
    if args.count("--text") > 1:
        raise ValueError("参数 --text 不能重复。")
    text_only = bool(args and args[-1] == "--text")
    if "--text" in args and not text_only:
        raise ValueError("参数 --text 必须放在末尾。")
    if text_only:
        args.pop()
    for argument in args:
        if argument.startswith("--"):
            raise ValueError(f"未知选项：{argument}。")
    return args, text_only


class Main(Star):
    """Provide diagnostics without model calls or conversation mutations."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        if not isinstance(config, dict):
            raise ValueError("开发助手配置必须是对象。")
        config = dict(config)
        if config.pop("browser_executable", ""):
            self.logger.warning(
                "dev_helper browser_executable is now configured by astrbot_plugin_browser."
            )
        count_fields = {"chatlog_default_count", "logs_default_count"}
        terminal_fields = {
            "terminal_max_pages": (5, 1, 20),
            "terminal_timeout": (60, 1, 600),
            "terminal_max_output_bytes": (1048576, 1024, 10485760),
        }
        fields = count_fields | terminal_fields.keys()
        if unknown := config.keys() - fields:
            raise ValueError(
                "开发助手存在未知配置项，请移除："
                + "、".join(sorted(map(str, unknown)))
            )
        if missing := count_fields - config.keys():
            raise ValueError("开发助手缺少配置项：" + "、".join(sorted(missing)))
        for field in sorted(count_fields):
            value = config[field]
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError(f"开发助手配置 {field} 必须是 1–100 的整数。")
        self.chatlog_default_count = config["chatlog_default_count"]
        self.logs_default_count = config["logs_default_count"]
        for field, (default, minimum, maximum) in terminal_fields.items():
            value = config.get(field, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(
                    f"开发助手配置 {field} 必须是 {minimum}–{maximum} 的整数。"
                )
            setattr(self, field, value)
        self.terminal = TerminalRunner(
            timeout=self.terminal_timeout, max_bytes=self.terminal_max_output_bytes
        )
        self.picture_renderer = LocalPictureRenderer(self.get_browser_service)
        self.context_usage = ContextUsage(self)
        self.ready = False
        self._restart_confirmations: dict[tuple[str, str], float] = {}
        self._restart_started = False
        self._plugin_removals: dict[
            tuple[str, str], tuple[float, str, str, object, bool]
        ] = {}

    def get_browser_service(self):
        metadata = self.context.get_registered_star("astrbot_plugin_browser")
        plugin = metadata.star_cls if metadata and metadata.activated else None
        service = getattr(plugin, "service", None)
        if service is None:
            raise RenderError("浏览器服务不可用，请启用 astrbot_plugin_browser 插件。")
        return service

    async def initialize(self) -> None:
        """Reject conflicting command registrations before enabling diagnostics."""
        conflicts = command_conflicts(self.__class__.__module__)
        if conflicts:
            raise RuntimeError("开发助手命令冲突：" + "、".join(conflicts))
        self.ready = True

    async def reply(self, event: AstrMessageEvent, text: str) -> None:
        """Submit sanitized plain text to AstrBot's standard response pipeline.

        Args:
            event: Real platform event used to reply.
            text: Diagnostic response, sanitized before submission.
        """
        event.should_call_llm(False)
        event.set_result(
            event.plain_result(redact(text)).use_t2i(False).use_markdown(False)
        )

    async def authorize(self, event: AstrMessageEvent, private: bool = False) -> bool:
        """Check caller permissions before reading data or parsing arguments.

        Args:
            event: Authenticated command event.
            private: Whether the command requires a private conversation.

        Returns:
            Whether the command may proceed.
        """
        if (
            not event.is_admin()
            or event.get_extra("_api_key_allow_admin_role") is False
        ):
            await self.reply(event, "开发助手仅限 AstrBot 管理员使用。")
            return False
        if private and not event.is_private_chat():
            await self.reply(event, "请在管理员私聊中使用日志查询。")
            return False
        if not self.ready:
            await self.reply(event, "开发助手尚未完成初始化，暂不可用。")
            return False
        conflicts = command_conflicts(self.__class__.__module__)
        if conflicts:
            await self.reply(event, "开发助手命令冲突：" + "、".join(conflicts))
            return False
        return True

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "restart",
        desc="确认后重启 AstrBot：restart，然后 restart confirm。仅限管理员私聊。",
    )
    async def restart(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Restart AstrBot after the same administrator confirms in private chat."""
        if not await self.authorize(event):
            yield
            event.stop_event()
            return
        if not event.is_private_chat():
            await self.reply(event, "请在管理员私聊中使用重启命令。")
            yield
            event.stop_event()
            return
        if is_desktop_managed_backend() and os.name != "nt":
            await self.reply(event, "桌面版脚本重启目前仅支持 Windows。")
            yield
            event.stop_event()
            return
        key = (event.unified_msg_origin, event.get_sender_id())
        action = arguments.strip()
        if not action:
            self._restart_confirmations[key] = time.monotonic() + CONFIRM_SECONDS
            target = (
                "整个 AstrBot Desktop 应用"
                if is_desktop_managed_backend()
                else "AstrBot"
            )
            await self.reply(
                event, f"确认重启{target}？请在 60 秒内发送 /restart confirm。"
            )
            yield
            event.stop_event()
            return
        if action != "confirm":
            await self.reply(
                event, "用法：/restart，然后在 60 秒内发送 /restart confirm。"
            )
            yield
            event.stop_event()
            return

        deadline = self._restart_confirmations.pop(key, None)
        if deadline is None or time.monotonic() >= deadline:
            await self.reply(event, "重启确认已过期，请重新发送 /restart。")
            yield
            event.stop_event()
            return
        if self._restart_started:
            await self.reply(event, "AstrBot 重启任务已启动。")
            yield
            event.stop_event()
            return

        self._restart_started = True
        await self.reply(event, "已确认，AstrBot 即将重启。")
        yield
        try:
            if is_desktop_managed_backend():
                schedule_desktop_restart()
            else:
                threading.Thread(
                    target=restart_process, name="restart", daemon=True
                ).start()
        except Exception:
            self._restart_started = False
            self.logger.exception("Failed to start AstrBot restart task.")
            await self.reply(event, "启动重启任务失败，请检查 AstrBot 日志。")
            yield
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "plugin remove",
        desc="确认后卸载插件：plugin remove <插件名> [--all]。仅限管理员私聊。",
    )
    async def plugin_remove(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Uninstall a plugin through the same service used by the WebUI."""
        if not await self.authorize(event):
            yield
            event.stop_event()
            return
        if not event.is_private_chat():
            await self.reply(event, "请在管理员私聊中卸载插件。")
            yield
            event.stop_event()
            return

        tokens = arguments.split()
        usage = (
            "用法：/plugin remove <插件名> [--all]；"
            "确认时发送 /plugin remove <插件名> [--all] confirm。"
        )
        if not tokens or tokens[1:] not in (
            [],
            ["--all"],
            ["confirm"],
            ["--all", "confirm"],
        ):
            await self.reply(event, usage)
            yield
            event.stop_event()
            return
        delete_all = "--all" in tokens[1:]
        confirming = tokens[-1] == "confirm" and len(tokens) > 1

        manager = self.context._star_manager
        if manager is None:
            await self.reply(event, "插件管理器不可用，无法卸载插件。")
            yield
            event.stop_event()
            return
        from astrbot.dashboard.services.plugin_service import PluginService

        service = PluginService(None, manager)
        name = tokens[0]
        plugin = service.find_plugin_by_name(name)
        failed_plugins = manager.failed_plugin_dict
        failed = plugin is None and name in failed_plugins
        if plugin is None and not failed:
            await self.reply(event, f"未找到插件 {name}。")
            yield
            event.stop_event()
            return
        if plugin is not None and plugin.reserved:
            await self.reply(event, "该插件是 AstrBot 保留插件，无法卸载。")
            yield
            event.stop_event()
            return

        canonical_name = name if failed else plugin.name
        kind = "failed" if failed else "loaded"
        identity = failed_plugins[name] if failed else plugin
        key = (event.unified_msg_origin, event.get_sender_id())
        if not confirming:
            self._plugin_removals[key] = (
                time.monotonic() + CONFIRM_SECONDS,
                canonical_name,
                kind,
                identity,
                delete_all,
            )
            scope = (
                "将删除插件文件、配置和数据。"
                if delete_all
                else "将删除插件文件，保留配置和数据。"
            )
            option = " --all" if delete_all else ""
            await self.reply(
                event,
                f"确认卸载 {canonical_name}？{scope}"
                f"请在 60 秒内发送 /plugin remove {canonical_name}{option} confirm。",
            )
            yield
            event.stop_event()
            return

        pending = self._plugin_removals.pop(key, None)
        if (
            pending is None
            or time.monotonic() >= pending[0]
            or canonical_name != pending[1]
            or kind != pending[2]
            or identity is not pending[3]
            or delete_all != pending[4]
        ):
            await self.reply(event, "卸载确认已过期或插件状态已变化，请重新发送命令。")
            yield
            event.stop_event()
            return

        await self.reply(event, f"正在卸载插件 {canonical_name}。")
        yield
        try:
            payload = {"delete_config": delete_all, "delete_data": delete_all}
            if failed:
                await service.uninstall_failed_plugin(
                    {"dir_name": canonical_name, **payload}
                )
            else:
                await service.uninstall_plugin({"name": canonical_name, **payload})
        except Exception as exc:
            self.logger.exception("Failed to uninstall plugin %s.", canonical_name)
            await self.reply(event, f"卸载插件 {canonical_name} 失败：{exc}")
        else:
            await self.reply(event, f"插件 {canonical_name} 已卸载。")
        yield
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "term", desc="执行系统终端命令并返回图片：term <命令>。仅限管理员私聊。"
    )
    async def term(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        if not await self.authorize(event):
            yield
            event.stop_event()
            return
        if not event.is_private_chat():
            await self.reply(event, "请在管理员私聊中使用终端。")
            yield
            event.stop_event()
            return
        # AstrBot's GreedyStr normalizes whitespace; preserve the original shell text.
        raw = event.get_message_str()
        command = arguments.strip()
        if raw == "term" or (raw.startswith("term") and raw[4:5].isspace()):
            command = raw[4:].strip()
        if not command or "\x00" in command:
            await self.reply(event, "用法：/term <命令>。命令不能包含 NUL 字符。")
            yield
            event.stop_event()
            return
        event.should_call_llm(False)
        try:
            # Check rendering availability before running a potentially mutating command.
            self.get_browser_service()
            root = await session_workspace(self.context, event.unified_msg_origin)
            result = await self.terminal.execute(
                (event.unified_msg_origin, event.get_sender_id()), root, command
            )
        except RenderError as error:
            await self.reply(event, str(error))
            yield
            event.stop_event()
            return
        except Exception:
            self.logger.error("Terminal execution failed before producing a result.")
            await self.reply(
                event, "终端执行失败，请检查工作目录和系统 shell 是否可用。"
            )
            yield
            event.stop_event()
            return
        attachment = False
        try:
            document = await asyncio.to_thread(result.document, self.terminal_max_pages)
            images = await self.picture_renderer.render(document)
        except RenderError as error:
            await self.reply(event, f"命令已执行。{error}\n执行结果将以文本附件发送。")
            yield
            images = []
            attachment = True
        for picture in images:
            event.set_result(
                event.chain_result([Image.fromBytes(picture)])
                .use_t2i(False)
                .use_markdown(False)
            )
            yield
            if event.is_stopped():
                return
        attachment |= (
            getattr(images, "total_pages", len(images)) > self.terminal_max_pages
        )
        if attachment:
            # Standard delivery consumes the file while the generator is suspended.
            with tempfile.TemporaryDirectory(
                prefix="astrbot-terminal-result-"
            ) as folder:
                path = Path(folder) / "terminal-output.txt"
                transcript = await asyncio.to_thread(result.transcript)
                await asyncio.to_thread(path.write_text, transcript, encoding="utf-8")
                event.set_result(
                    event.chain_result([File(name=path.name, file=str(path))])
                    .use_t2i(False)
                    .use_markdown(False)
                )
                yield
        event.stop_event()

    async def reply_diagnostic(
        self, event: AstrMessageEvent, text: str, document: PictureDocument | None
    ) -> AsyncIterator[None]:
        """Render all pages before delivery or submit the same query as plain text."""
        event.should_call_llm(False)
        if document is None:
            await self.reply(event, text)
            yield
            return
        try:
            images = await self.picture_renderer.render(document)
        except RenderError as error:
            self.logger.warning(
                "Diagnostic image rendering failed; using text: %s", redact(str(error))
            )
            await self.reply(event, "图片渲染不可用，已改为文本。\n\n" + text)
            yield
            return
        for image in images:
            event.set_result(
                event.chain_result([Image.fromBytes(image)])
                .use_t2i(False)
                .use_markdown(False)
            )
            yield
            if event.is_stopped():
                return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "dev",
        desc="查看开发助手帮助、命令与模型工具；支持 commands、tools、plugin 子命令。",
    )
    async def dev(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Let AstrBot deliver the result before stopping further handlers."""
        await self._dev(event, arguments)
        yield
        event.stop_event()

    async def _dev(self, event: AstrMessageEvent, arguments: str) -> None:
        """Read catalogs using optional positional page numbers."""
        if not await self.authorize(event):
            return
        args = arguments.split()
        try:
            if not args:
                commands = command_entries()
                tools = tool_entries(self.context.get_llm_tool_manager())
                await self.reply(
                    event,
                    f"开发助手\n已注册命令（含指令组）：{len(commands)} 个\n模型工具：{len(tools)} 个\n"
                    + HELP,
                )
                return
            if args[0] in {"commands", "tools"} and len(args) <= 2:
                page = positive_number(args[1] if len(args) == 2 else "", 1)
                entries = (
                    command_entries()
                    if args[0] == "commands"
                    else tool_entries(self.context.get_llm_tool_manager())
                )
                title = "命令目录" if args[0] == "commands" else "模型工具目录"
                next_command = f"/dev {args[0]}"
            elif args[0] == "plugin" and 2 <= len(args) <= 3:
                page = positive_number(args[2] if len(args) == 3 else "", 1)
                matches = [
                    plugin
                    for plugin in self.context.get_all_stars()
                    if plugin.name == args[1]
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "插件标识不唯一。"
                        if matches
                        else "插件不存在或未载入，无法取得命令与工具信息。"
                    )
                plugin = matches[0]
                entries = [
                    item
                    for item in command_entries()
                    + tool_entries(self.context.get_llm_tool_manager())
                    if item.plugin == plugin.name and item.source != "MCP"
                ]
                title = (
                    f"插件：{plugin.name}（{'启用' if plugin.activated else '停用'}）"
                )
                next_command = f"/dev plugin {args[1]}"
                if not entries:
                    message = (
                        "当前没有已注册的命令或工具。"
                        if plugin.activated
                        else "插件已停用，命令与工具信息不可用。"
                    )
                    await self.reply(event, title + "\n" + message)
                    return
            else:
                raise ValueError("用法：\n" + DEV_USAGE)
            pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
            if page > pages:
                raise ValueError(f"页码超出范围，共 {pages} 页。")
            selected = entries[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
            lines = [
                f"{title}｜共 {len(entries)} 项｜第 {page}/{pages} 页",
                "状态来自注册信息，会话权限及实际请求仍需另行判定。",
            ]
            lines.extend(entry.render() for entry in selected)
            if not selected:
                lines.append("当前没有已注册的条目。")
            if page < pages:
                lines.append(f"下一页：{next_command} {page + 1}")
            await self.reply(event, "\n\n".join(lines))
        except ValueError as error:
            await self.reply(event, str(error))
        except Exception:
            self.logger.exception("Failed to read the command/tool catalog.")
            await self.reply(event, "命令或工具目录读取失败，请检查 AstrBot 日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "logs",
        desc="最近日志默认输出图片：logs [条目数] [--text] 或 logs warning [条目数] [--text]。仅限管理员私聊。",
    )
    async def logs(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        result = await self._logs(event, arguments)
        if result is None:
            yield
        else:
            async for _ in self.reply_diagnostic(event, *result):
                yield
        event.stop_event()

    async def _logs(
        self, event: AstrMessageEvent, arguments: str
    ) -> tuple[str, PictureDocument | None] | None:
        """Read the existing log cache, filtering levels before taking the tail."""
        if not await self.authorize(event, private=True):
            return
        try:
            args, text_only = output_arguments(arguments)
            warning_only = bool(args and args[0] == "warning")
            if warning_only:
                args = args[1:]
            if len(args) > 1:
                raise ValueError("参数过多。")
            count = positive_number(
                args[0] if args else "", self.logs_default_count, 100
            )
            brokers = {
                id(handler.log_broker): handler.log_broker
                for handler in logging.getLogger("astrbot").handlers
                if isinstance(handler, LogQueueHandler)
            }
            if len(brokers) != 1:
                await self.reply(event, "日志源不可用：未找到唯一的 AstrBot 日志缓存。")
                return
            broker = next(iter(brokers.values()))
            records = list(broker.log_cache)
            if not records:
                await self.reply(event, "当前日志缓存为空。")
                return
            filtered = [
                record
                for record in records
                if not warning_only
                or record["level"] in {"WARNING", "ERROR", "CRITICAL"}
            ]
            if not filtered:
                await self.reply(event, "当前缓存内无 WARNING 及以上日志。")
                return
            selected = filtered[-count:]
            lines = [
                f"最近日志｜当前缓存 {len(records)}/{broker.log_cache.maxlen} 条｜符合条件 {len(filtered)} 条｜显示最近 {len(selected)} 条",
                "级别：WARNING 及以上" if warning_only else "级别：全部",
                "",
            ]
            selected_text = [
                redact(record["data"].rstrip("\r\n")) for record in selected
            ]
            document = None
            if not text_only:
                document = PictureDocument(
                    title="最近日志",
                    summary=redact("\n".join(lines[:2])),
                    blocks=tuple(
                        PictureBlock(
                            text,
                            "log",
                            record["level"],
                        )
                        for record, text in zip(selected, selected_text)
                    ),
                )
            lines.extend(selected_text)
            return "\n".join(lines), document
        except ValueError as error:
            await self.reply(event, f"{error}\n用法：\n{LOG_USAGE}")
        except Exception:
            self.logger.exception("Failed to read the AstrBot log cache.")
            await self.reply(event, "日志源数据读取失败，请检查 AstrBot 日志服务。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "chatlog",
        desc="当前会话记录默认输出 JSON 高亮图片：chatlog [条目数] [--text]。省略条目数时使用插件配置。",
    )
    async def chatlog(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        result = await self._chatlog(event, arguments)
        if result is None:
            yield
        else:
            async for _ in self.reply_diagnostic(event, *result):
                yield
        event.stop_event()

    async def _chatlog(
        self, event: AstrMessageEvent, arguments: str
    ) -> tuple[str, PictureDocument | None] | None:
        """Read the current saved history without creating or changing a session."""
        if not await self.authorize(event):
            return
        try:
            args, text_only = output_arguments(arguments)
            if len(args) > 1:
                raise ValueError("参数过多。")
            count = positive_number(
                args[0] if args else "", self.chatlog_default_count, 100
            )
        except ValueError as error:
            await self.reply(event, f"{error}\n用法：{CHATLOG_USAGE}")
            return
        try:
            origin = event.unified_msg_origin
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(origin)
            if not cid:
                await self.reply(event, "当前会话没有选中的对话。")
                return
            conversation = await manager.get_conversation(
                origin, cid, create_if_not_exists=False
            )
            if conversation is None:
                await self.reply(event, "当前会话选中的对话记录不存在。")
                return
            if (
                conversation.user_id != origin
                or conversation.platform_id != event.get_platform_id()
            ):
                await self.reply(event, "对话归属与当前会话不一致，已拒绝读取。")
                return
            records = json.loads(conversation.history)
            if not isinstance(records, list) or any(
                not isinstance(record, dict) or not isinstance(record.get("role"), str)
                for record in records
            ):
                raise ValueError("Invalid stored history shape.")
            if not records:
                await self.reply(
                    event, f"当前对话记录为空。\n会话：{origin}\n对话：{cid}"
                )
                return
            selected = records[-count:]
            lines = [
                f"当前会话记录\n会话：{origin}\n对话：{cid}\n共 {len(records)} 条，显示最近 {len(selected)} 条（按消息计数）"
            ]
            selected_text = [history_text(record) for record in selected]
            document = None
            if not text_only:
                document = PictureDocument(
                    title="当前会话记录 · JSON",
                    summary=redact(
                        f"会话：{origin}\n对话：{cid}\n共 {len(records)} 条，显示第 "
                        f"{len(records) - len(selected) + 1}–{len(records)} 条（按消息计数）\n"
                        "仅显示已保存记录，可能不含尚未落库的消息及动态系统提示词。"
                    ),
                    blocks=(
                        PictureBlock(
                            json.dumps(
                                [json.loads(text) for text in selected_text],
                                ensure_ascii=False,
                                indent=2,
                            ),
                            "json",
                        ),
                    ),
                )
            for index, (record, text) in enumerate(
                zip(selected, selected_text), len(records) - len(selected) + 1
            ):
                lines.append(f"#{index} {record['role']}\n{text}")
            lines.append(
                "以上为已保存的会话记录，可能不含尚未落库的消息及动态系统提示词。"
            )
            return "\n\n".join(lines), document
        except (ValueError, TypeError):
            self.logger.exception("Stored conversation history is malformed.")
            await self.reply(event, "当前对话记录数据格式错误，无法读取。")
        except Exception:
            self.logger.exception("Failed to read the current conversation.")
            await self.reply(event, "当前对话读取失败，请检查 AstrBot 存储与日志。")

    async def _usage_conversation(self, event: AstrMessageEvent, cid=None):
        origin = event.unified_msg_origin
        manager = self.context.conversation_manager
        cid = cid or await manager.get_curr_conversation_id(origin)
        if not cid:
            return None, None, []
        conversation = await manager.get_conversation(
            origin, cid, create_if_not_exists=False
        )
        if conversation is None:
            return cid, None, []
        if (
            conversation.user_id != origin
            or conversation.platform_id != event.get_platform_id()
        ):
            raise ValueError("对话归属与当前会话不一致，已拒绝读取。")
        history = json.loads(conversation.history)
        if not isinstance(history, list) or any(
            not isinstance(item, dict) for item in history
        ):
            raise ValueError("当前对话记录数据格式错误，无法读取。")
        return cid, conversation, history

    @filter.on_llm_request()
    async def capture_context_start(
        self, event: AstrMessageEvent, request: ProviderRequest
    ):
        if not self.ready or request.conversation is None:
            return
        try:
            cid, conversation, history = await self._usage_conversation(
                event, request.conversation.cid
            )
            if conversation is not None:
                ticket = await self.context_usage.begin(
                    event.unified_msg_origin, cid, history
                )
                event.set_extra("dev_helper_context_ticket", ticket)
        except Exception:
            self.logger.exception("Failed to prepare context usage recording.")

    @filter.on_agent_done()
    async def capture_context_finish(
        self, event: AstrMessageEvent, run_context, response
    ):
        ticket = event.get_extra("dev_helper_context_ticket")
        if not self.ready or ticket is None or response is None or response.is_chunk:
            return
        if response.role != "assistant" or ticket["origin"] != event.unified_msg_origin:
            return
        try:
            messages = [
                message
                for message in run_context.messages
                if not (message.role in {"user", "assistant"} and message._no_save)
            ]
            # The last user message and subsequent tool/assistant messages identify
            # this turn in saved context, including repeated identical answers.
            start = next(
                (
                    i
                    for i in range(len(messages) - 1, -1, -1)
                    if messages[i].role == "user"
                ),
                None,
            )
            if start is not None:
                anchor = history_hashes(
                    dump_messages_with_checkpoints(messages[start:])
                )
                await self.context_usage.finish(ticket, anchor, response)
        except Exception:
            self.logger.exception("Failed to record context usage.")

    @filter.after_message_sent()
    async def clear_context_usage(self, event: AstrMessageEvent):
        # Core sets this only after a successful reset/new, including renamed
        # commands. Other clear operations are reconciled on query or request.
        if not self.ready or not event.get_extra("_clean_group_context_session", False):
            return
        try:
            cid, _, history = await self._usage_conversation(event)
            if cid and not history:
                async with self.context_usage.lock:
                    await self.context_usage.sync(
                        self.context_usage.key(event.unified_msg_origin, cid),
                        history,
                        force_clear=True,
                    )
        except Exception:
            self.logger.exception("Failed to clear context usage after context reset.")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "ctx", desc="上下文用量默认输出表格图片：ctx [轮数] [--text]，默认 5 轮。"
    )
    async def ctx(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        result = await self._ctx(event, arguments)
        if result is None:
            yield
        else:
            async for _ in self.reply_diagnostic(event, *result):
                yield
        event.stop_event()

    async def _ctx(
        self, event: AstrMessageEvent, arguments: str
    ) -> tuple[str, PictureDocument | None] | None:
        if not await self.authorize(event):
            return
        try:
            args, text_only = output_arguments(arguments)
            if len(args) > 1:
                raise ValueError("参数过多。")
            count = positive_number(args[0] if args else "", 5, 100)
        except ValueError as error:
            await self.reply(event, f"{error}\n用法：{CTX_USAGE}")
            return
        try:
            cid, conversation, history = await self._usage_conversation(event)
            if cid is None:
                await self.reply(event, "当前会话没有选中的对话。")
                return
            async with self.context_usage.lock:
                _, records = await self.context_usage.sync(
                    self.context_usage.key(event.unified_msg_origin, cid),
                    history,
                )
            if conversation is None:
                await self.reply(event, "当前会话选中的对话记录不存在。")
                return
            if not history:
                await self.reply(
                    event, "当前对话上下文为空，暂无已保存轮次的用量记录。"
                )
                return
            lines = ["上下文用量（tokens）"]
            document = None
            if records:
                selected = records[-count:]
                if not text_only:
                    document = usage_picture(selected)
                lines[0] += f"｜最近 {len(selected)} 轮"
                lines.extend(format_round(record) for record in selected)
            else:
                total = conversation.token_usage
                if type(total) is int and total > 0:
                    if not text_only:
                        document = usage_picture([], total)
                    lines.append(f"最近一次合计：{total:,}（无明细）")
                else:
                    lines.append("暂无用量记录。")
            return "\n\n".join(lines), document
        except ValueError as error:
            await self.reply(event, str(error))
        except Exception:
            self.logger.exception("Failed to query context usage.")
            await self.reply(event, "上下文用量读取失败，请检查 AstrBot 日志。")

    async def terminate(self) -> None:
        """Mark diagnostics unavailable while AstrBot unloads the plugin."""
        self.ready = False
        await self.terminal.close()
