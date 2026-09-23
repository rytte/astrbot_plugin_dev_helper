"""Administrator diagnostics for AstrBot."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import dump_messages_with_checkpoints
from astrbot.core.log import LogQueueHandler
from astrbot.core.star.filter.command import GreedyStr

from .catalog import command_conflicts, command_entries, tool_entries
from .context_usage import ContextUsage, format_round, history_hashes, usage_picture
from .display import history_text, positive_number, redact
from .pictures import LocalPictureRenderer, PictureBlock, PictureDocument, RenderError

INSPECT_USAGE = (
    "/inspect commands [页码]\n/inspect tools [页码]\n/inspect plugin <插件标识> [页码]"
)
LOG_USAGE = "/logs [条目数]\n/logs warning [条目数]"
LOG_PIC_USAGE = "/logs-pic [条目数]\n/logs-pic warning [条目数]"
HELP = (
    "\n查询\n"
    + INSPECT_USAGE
    + "\n\n日志与对话\n"
    + LOG_USAGE
    + "\n/chatlog [条目数]"
    + "\n/ctx [轮数]"
    + "\n\n图片输出\n"
    + LOG_PIC_USAGE
    + "\n/chatlog-pic [条目数]"
    + "\n/ctx-pic [轮数]"
)
PAGE_SIZE = 20


class Main(Star):
    """Provide diagnostics without model calls or conversation mutations."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        if not isinstance(config, dict):
            raise ValueError("开发助手配置必须是对象。")
        count_fields = {"chatlog_default_count", "logs_default_count"}
        fields = count_fields | {"browser_executable"}
        if unknown := config.keys() - fields:
            raise ValueError(
                "开发助手存在未知配置项，请移除："
                + "、".join(sorted(map(str, unknown)))
            )
        if missing := fields - config.keys():
            raise ValueError("开发助手缺少配置项：" + "、".join(sorted(missing)))
        for field in sorted(count_fields):
            value = config[field]
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError(f"开发助手配置 {field} 必须是 1–100 的整数。")
        self.chatlog_default_count = config["chatlog_default_count"]
        self.logs_default_count = config["logs_default_count"]
        self.picture_renderer = LocalPictureRenderer(config["browser_executable"])
        self.context_usage = ContextUsage(self)
        self.ready = False

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

    async def reply_pictures(
        self, event: AstrMessageEvent, document: PictureDocument
    ) -> AsyncIterator[None]:
        """Render all pages locally, then yield each image for standard delivery."""
        event.should_call_llm(False)
        try:
            images = await self.picture_renderer.render(document)
        except RenderError as error:
            await self.reply(event, str(error))
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
        "inspect",
        desc="查看开发助手帮助、命令与模型工具；支持 commands、tools、plugin 子命令。",
    )
    async def inspect(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Let AstrBot deliver the result before stopping further handlers."""
        await self._inspect(event, arguments)
        yield
        event.stop_event()

    async def _inspect(self, event: AstrMessageEvent, arguments: str) -> None:
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
                next_command = f"/inspect {args[0]}"
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
                next_command = f"/inspect plugin {args[1]}"
                if not entries:
                    message = (
                        "当前没有已注册的命令或工具。"
                        if plugin.activated
                        else "插件已停用，命令与工具信息不可用。"
                    )
                    await self.reply(event, title + "\n" + message)
                    return
            else:
                raise ValueError("用法：\n" + INSPECT_USAGE)
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
        desc="查看最近日志：logs [条目数] 或 logs warning [条目数]。仅限管理员私聊。",
    )
    async def logs(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Let AstrBot deliver the result before stopping further handlers."""
        await self._logs(event, arguments)
        yield
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "logs-pic",
        desc="日志等级配色图片：logs-pic [条目数] 或 logs-pic warning [条目数]。仅限管理员私聊。",
    )
    async def logs_pic(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        document = await self._logs(event, arguments, picture=True)
        if document is None:
            yield
        else:
            async for _ in self.reply_pictures(event, document):
                yield
        event.stop_event()

    async def _logs(
        self, event: AstrMessageEvent, arguments: str, picture: bool = False
    ) -> PictureDocument | None:
        """Read the existing log cache, filtering levels before taking the tail."""
        if not await self.authorize(event, private=True):
            return
        args = arguments.split()
        usage = LOG_PIC_USAGE if picture else LOG_USAGE
        try:
            warning_only = bool(args and args[0] == "warning")
            if warning_only:
                args = args[1:]
            if len(args) > 1:
                raise ValueError("用法：\n" + usage)
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
            if picture:
                return PictureDocument(
                    title="最近日志",
                    summary=redact("\n".join(lines[:2])),
                    blocks=tuple(
                        PictureBlock(
                            redact(record["data"].rstrip("\r\n")),
                            "log",
                            record["level"],
                        )
                        for record in selected
                    ),
                )
            for record in selected:
                lines.append(redact(record["data"].rstrip("\r\n")))
            await self.reply(event, "\n".join(lines))
        except ValueError as error:
            await self.reply(event, f"{error}\n用法：\n{usage}")
        except Exception:
            self.logger.exception("Failed to read the AstrBot log cache.")
            await self.reply(event, "日志源数据读取失败，请检查 AstrBot 日志服务。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "chatlog",
        desc="查看当前会话最近的已保存记录：chatlog [条目数]。省略条目数时使用插件配置。",
    )
    async def chatlog(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        """Let AstrBot deliver the result before stopping further handlers."""
        await self._chatlog(event, arguments)
        yield
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command(
        "chatlog-pic",
        desc="当前会话 JSON 高亮图片：chatlog-pic [条目数]。省略条目数时使用插件配置。",
    )
    async def chatlog_pic(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        document = await self._chatlog(event, arguments, picture=True)
        if document is None:
            yield
        else:
            async for _ in self.reply_pictures(event, document):
                yield
        event.stop_event()

    async def _chatlog(
        self, event: AstrMessageEvent, arguments: str, picture: bool = False
    ) -> PictureDocument | None:
        """Read the current saved history without creating or changing a session."""
        if not await self.authorize(event):
            return
        try:
            args = arguments.split()
            if len(args) > 1:
                command = "chatlog-pic" if picture else "chatlog"
                raise ValueError(f"用法：/{command} [条目数]")
            count = positive_number(
                args[0] if args else "", self.chatlog_default_count, 100
            )
        except ValueError as error:
            await self.reply(event, str(error))
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
            if picture:
                return PictureDocument(
                    title="当前会话记录 · JSON",
                    summary=redact(
                        f"会话：{origin}\n对话：{cid}\n共 {len(records)} 条，显示第 "
                        f"{len(records) - len(selected) + 1}–{len(records)} 条（按消息计数）\n"
                        "仅显示已保存记录，可能不含尚未落库的消息及动态系统提示词。"
                    ),
                    blocks=(
                        PictureBlock(
                            json.dumps(
                                [
                                    json.loads(history_text(record))
                                    for record in selected
                                ],
                                ensure_ascii=False,
                                indent=2,
                            ),
                            "json",
                        ),
                    ),
                )
            for index, record in enumerate(selected, len(records) - len(selected) + 1):
                lines.append(f"#{index} {record['role']}\n{history_text(record)}")
            lines.append(
                "以上为已保存的会话记录，可能不含尚未落库的消息及动态系统提示词。"
            )
            await self.reply(event, "\n\n".join(lines))
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
        "ctx", desc="查看当前对话最近几轮的上下文用量：ctx [轮数]，默认 5 轮。"
    )
    async def ctx(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        await self._ctx(event, arguments)
        yield
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ctx-pic", desc="上下文用量图片：ctx-pic [轮数]，默认 5 轮。")
    async def ctx_pic(
        self, event: AstrMessageEvent, arguments: GreedyStr
    ) -> AsyncIterator[None]:
        document = await self._ctx(event, arguments, picture=True)
        if document is not None:
            async for _ in self.reply_pictures(event, document):
                yield
        else:
            yield
        event.stop_event()

    async def _ctx(
        self, event: AstrMessageEvent, arguments: str, picture: bool = False
    ) -> PictureDocument | None:
        if not await self.authorize(event):
            return
        usage = "用法：/ctx-pic [轮数]" if picture else "用法：/ctx [轮数]"
        try:
            args = arguments.split()
            if len(args) > 1:
                raise ValueError(usage)
            count = positive_number(args[0] if args else "", 5, 100)
        except ValueError as error:
            await self.reply(event, f"{error}\n{usage}")
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
            if records:
                selected = records[-count:]
                if picture:
                    return usage_picture(selected)
                lines[0] += f"｜最近 {len(selected)} 轮"
                lines.extend(format_round(record) for record in selected)
            else:
                total = conversation.token_usage
                if type(total) is int and total > 0:
                    if picture:
                        return usage_picture([], total)
                    lines.append(f"最近一次合计：{total:,}（无明细）")
                else:
                    lines.append("暂无用量记录。")
            await self.reply(event, "\n\n".join(lines))
        except ValueError as error:
            await self.reply(event, str(error))
        except Exception:
            self.logger.exception("Failed to query context usage.")
            await self.reply(event, "上下文用量读取失败，请检查 AstrBot 日志。")

    async def terminate(self) -> None:
        """Mark diagnostics unavailable while AstrBot unloads the plugin."""
        self.ready = False
