"""Usage semantics and lifecycle against real AstrBot hooks and history saving."""

import asyncio
import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.agent.response import AgentStats
from astrbot.core.astr_agent_hooks import MAIN_AGENT_HOOKS
from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
    InternalAgentSubStage,
)
from astrbot.core.provider.entities import TokenUsage
from astrbot.core.star.star_handler import EventType
from astrbot_plugin_dev_helper.context_usage import (
    ContextUsage,
    extract_usage,
    usage_picture,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            {
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 30,
                    "total_tokens": 1030,
                    "prompt_tokens_details": {"cached_tokens": 800},
                    "completion_tokens_details": {"reasoning_tokens": 10},
                }
            },
            {
                "input": 1000,
                "output": 30,
                "total": 1030,
                "cached": 800,
                "reasoning": 10,
            },
        ),
        (
            {
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 0},
                }
            },
            {"input": 200, "output": 40, "total": 240, "cached": 0},
        ),
        ({"usage": {"total_tokens": 700}}, {"total": 700}),
        ({"usage": {"completion_tokens": 9, "prompt_tokens": None}}, {"output": 9}),
        ({"usage": {"input_tokens": 90}}, {"input": 90}),
        ({"usage": {"input_tokens": -1, "output_tokens": True}}, {}),
        (
            {
                "usage_metadata": {
                    "prompt_token_count": 200,
                    "candidates_token_count": 20,
                    "thoughts_token_count": 10,
                    "cached_content_token_count": 50,
                    "total_token_count": 230,
                }
            },
            {"input": 200, "output": 30, "reasoning": 10, "cached": 50, "total": 230},
        ),
        (
            {
                "type": "message",
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 30,
                },
            },
            {
                "input": 180,
                "output": 20,
                "total": 200,
                "uncached": 50,
                "cached": 100,
                "cache_write": 30,
            },
        ),
        (
            {"type": "message", "usage": {"input_tokens": 50, "output_tokens": 20}},
            {"uncached": 50, "output": 20},
        ),
    ],
)
def test_extract_only_available_provider_fields(raw, expected):
    response = LLMResponse(
        role="assistant", raw_completion=raw, usage=TokenUsage(9, 9, 9)
    )
    assert extract_usage(response)["values"] == expected


def test_sdk_usage_objects_and_unknown_normalized_defaults():
    response = LLMResponse(
        role="assistant",
        raw_completion=SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=50, completion_tokens=2)
        ),
    )
    assert extract_usage(response)["values"] == {"input": 50, "output": 2, "total": 52}
    response.raw_completion = None
    response.usage = TokenUsage()
    assert extract_usage(response) == {"values": {}, "source": "未提供用量"}
    response.usage = TokenUsage(input_other=100, input_cached=40, output=10)
    assert extract_usage(response)["values"] == {
        "input": 140,
        "cached": 40,
        "output": 10,
        "total": 150,
    }
    response.usage = TokenUsage(input_other=100, output=10)
    assert "cached" not in extract_usage(response)["values"]
    response.usage = TokenUsage(input_other=100)
    assert extract_usage(response)["values"] == {"input": 100}
    response.usage = TokenUsage(input_cached=100)
    assert extract_usage(response)["values"] == {"input": 100, "cached": 100}


@pytest.fixture
def usage_env(env):
    storage = {}

    async def get(key, default):
        return copy.deepcopy(storage.get(key, default))

    async def put(key, value):
        storage[key] = copy.deepcopy(value)

    async def delete(key):
        storage.pop(key, None)

    env.plugin.get_kv_data = AsyncMock(side_effect=get)
    env.plugin.put_kv_data = AsyncMock(side_effect=put)
    env.plugin.delete_kv_data = AsyncMock(side_effect=delete)
    conversation = SimpleNamespace(
        cid="cid",
        user_id=env.event().unified_msg_origin,
        platform_id="qq_one",
        history="[]",
        token_usage=0,
    )
    manager = env.context.conversation_manager
    manager.get_curr_conversation_id.return_value = "cid"
    manager.get_conversation.return_value = conversation

    async def update(origin, cid, history, token_usage=None):
        conversation.history = json.dumps(history)
        if token_usage is not None:
            conversation.token_usage = token_usage

    manager.update_conversation = AsyncMock(side_effect=update)
    stage = InternalAgentSubStage.__new__(InternalAgentSubStage)
    stage.conv_manager = manager

    async def turn(index, *, unknown=False, tools=False, persist=True, duplicate=False):
        event = env.event(f"question-{index}", user="member", admin=False)
        # Ordinary members' turns in the same shared session must be recorded too.
        event.unified_msg_origin = conversation.user_id
        request = ProviderRequest(prompt=f"question-{index}", conversation=conversation)
        await call_event_hook(event, EventType.OnLLMRequestEvent, request)
        messages = [Message(role="system", content="secret-system-prompt")]
        messages += [
            Message.model_validate(item)
            for item in json.loads(conversation.history)
            if item["role"] != "_checkpoint"
        ]
        messages.append(Message(role="user", content=f"question-{index}"))
        if tools:
            messages.extend(
                [
                    Message(
                        role="assistant",
                        tool_calls=[
                            {
                                "type": "function",
                                "id": "tool-call",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    ),
                    Message(
                        role="tool",
                        tool_call_id="tool-call",
                        content="private-tool-result",
                    ),
                ]
            )
        response = LLMResponse(
            role="assistant",
            completion_text=f"answer-{index}",
            usage=TokenUsage(100 * index, 10 * index, index) if not unknown else None,
        )
        messages.append(
            Message(role="assistant", content=[TextPart(text=response.completion_text)])
        )
        run_context = SimpleNamespace(
            context=SimpleNamespace(event=event), messages=messages
        )

        async def done():
            await MAIN_AGENT_HOOKS.on_agent_done(run_context, response)
            if duplicate:
                await MAIN_AGENT_HOOKS.on_agent_done(run_context, response)
            if persist:
                await stage._save_to_history(
                    event, request, response, messages, AgentStats()
                )

        await asyncio.create_task(done())
        return event, request, run_context, response

    env.usage_storage = storage
    env.conversation = conversation
    env.turn = turn
    return env


async def test_recent_rounds_real_hooks_history_and_no_cumulative_tool_usage(usage_env):
    env = usage_env
    for index in range(1, 8):
        await env.turn(index, tools=True, duplicate=True)
    event = env.event("/ctx", admin=False)
    await env.scheduler.execute(event)
    text = "".join(event.sent)
    assert "最近 5 轮" in text and "第 2 轮" not in text and "第 3 轮" in text
    assert "输入：770" in text
    assert "缓存读取：70" in text
    assert "合计：777" in text
    assert "question-" not in text and "answer-" not in text
    assert not env.model_calls and event.is_stopped()
    saved = json.dumps(env.usage_storage)
    assert all(
        secret not in saved
        for secret in (
            "question-",
            "answer-",
            "secret-system-prompt",
            "private-tool-result",
        )
    )
    env.plugin.context_usage = ContextUsage(env.plugin)
    event = env.event()
    await env.invoke("ctx", event, "2")
    assert "最近 2 轮" in "".join(event.sent)
    assert "第 5 轮" not in "".join(event.sent)


async def test_unknown_round_does_not_reuse_previous_usage(usage_env):
    env = usage_env
    await env.turn(1)
    await env.turn(2, unknown=True)
    event = env.event()
    await env.invoke("ctx", event, "1")
    text = "".join(event.sent)
    assert "第 2 轮" in text and "未提供用量" in text
    assert "输入：" not in text and "合计：" not in text


async def test_unsaved_response_is_not_shown_and_deleted(usage_env):
    env = usage_env
    await env.turn(1, persist=False)
    event = env.event()
    await env.invoke("ctx", event, "")
    assert "上下文为空" in "".join(event.sent)
    assert not env.usage_storage


async def test_actual_reset_clears_and_rejects_late_completion(usage_env, monkeypatch):
    from astrbot.builtin_stars.builtin_commands.commands import conversation as commands

    env = usage_env
    await env.turn(1)
    old_request_event = env.event()
    await call_event_hook(
        old_request_event,
        EventType.OnLLMRequestEvent,
        ProviderRequest(conversation=env.conversation),
    )
    old_ticket = old_request_event.get_extra("dev_helper_context_ticket")
    env.context.get_using_provider_async = AsyncMock(return_value=object())
    monkeypatch.setattr(commands.sp, "get_async", AsyncMock(return_value={}))
    reset = env.event("/a-renamed-reset")
    await commands.ConversationCommands(env.context).reset(reset)
    assert json.loads(env.conversation.history) == []
    await call_event_hook(reset, EventType.OnAfterMessageSentEvent)
    assert not env.usage_storage
    await env.plugin.context_usage.finish(
        old_ticket, ["late"], LLMResponse(role="assistant", usage=TokenUsage(90, 0, 1))
    )
    assert not env.usage_storage
    await env.turn(2)
    event = env.event()
    await env.invoke("ctx", event, "")
    text = "".join(event.sent)
    assert "第 1 轮" in text and "输入：220" in text and "输入：110" not in text


async def test_failed_reset_and_ordinary_commands_preserve_records(
    usage_env, monkeypatch
):
    from astrbot.builtin_stars.builtin_commands.commands import conversation as commands

    env = usage_env
    await env.turn(1)
    before = copy.deepcopy(env.usage_storage)
    env.context.get_using_provider_async = AsyncMock(return_value=None)
    monkeypatch.setattr(commands.sp, "get_async", AsyncMock(return_value={}))
    event = env.event("/reset")
    await commands.ConversationCommands(env.context).reset(event)
    assert not event.get_extra("_clean_group_context_session", False)
    await call_event_hook(event, EventType.OnAfterMessageSentEvent)
    assert env.usage_storage == before


async def test_webui_clear_and_compression_prune_by_actual_history(usage_env):
    env = usage_env
    for index in range(1, 4):
        await env.turn(index)
    env.conversation.history = json.dumps(json.loads(env.conversation.history)[-2:])
    event = env.event()
    await env.invoke("ctx", event, "")
    text = "".join(event.sent)
    assert "第 3 轮" in text and "第 2 轮" not in text
    assert len(next(iter(env.usage_storage.values()))["records"]) == 1
    env.conversation.history = "[]"
    env.plugin.context_usage = ContextUsage(env.plugin)
    await env.turn(4)
    event = env.event()
    await env.invoke("ctx", event, "")
    assert "第 1 轮" in "".join(event.sent) and "第 3 轮" not in "".join(event.sent)


async def test_different_conversation_and_wrong_owner_cannot_share_usage(usage_env):
    env = usage_env
    await env.turn(1)
    original = env.conversation.history
    env.conversation.cid = "other-cid"
    env.conversation.history = "[]"
    env.context.conversation_manager.get_curr_conversation_id.return_value = "other-cid"
    event = env.event()
    await env.invoke("ctx", event, "")
    assert "上下文为空" in "".join(event.sent)
    env.conversation.cid = "cid"
    env.conversation.history = original
    env.context.conversation_manager.get_curr_conversation_id.return_value = "cid"
    event = env.event()
    await env.invoke("ctx", event, "")
    assert "输入：110" in "".join(event.sent)
    env.conversation.user_id = "wrong-session"
    event = env.event()
    await env.invoke("ctx", event, "")
    assert "归属" in "".join(event.sent) and "输入：110" not in "".join(event.sent)


async def test_existing_conversation_only_reports_known_total(usage_env):
    env = usage_env
    env.conversation.history = '[{"role":"user","content":"old"}]'
    env.conversation.token_usage = 456
    event = env.event()
    await env.invoke("ctx", event, "")
    text = "".join(event.sent)
    assert "最近一次合计：456（无明细）" in text
    assert "第 1 轮" not in text


async def test_query_during_first_request_does_not_invalidate_recording(usage_env):
    env = usage_env
    event = env.event()
    await call_event_hook(
        event,
        EventType.OnLLMRequestEvent,
        ProviderRequest(conversation=env.conversation),
    )
    ticket = event.get_extra("dev_helper_context_ticket")
    query = env.event()
    await env.invoke("ctx", query, "")
    assert env.usage_storage[ticket["key"]]["generation"] == ticket["generation"]
    await env.plugin.context_usage.finish(
        ticket,
        ["unfinished"],
        LLMResponse(role="assistant", usage=TokenUsage(30, 0, 2)),
    )
    assert len(env.usage_storage[ticket["key"]]["records"]) == 1


@pytest.mark.parametrize("arguments,count", [("", 5), ("2", 2)])
async def test_ctx_picture_uses_same_recent_rounds_and_sends_pages(
    usage_env, arguments, count
):
    env = usage_env
    for index in range(1, 8):
        await env.turn(index)
    pages = [b"page-one", b"page-two"]
    env.plugin.picture_renderer.render = AsyncMock(return_value=pages)
    event = env.event(f"/ctx-pic {arguments}", admin=False)
    await env.scheduler.execute(event)
    document = env.plugin.picture_renderer.render.call_args.args[0]
    assert document.title == "上下文用量"
    assert len(document.table.rows) == count
    assert [row[0] for row in document.table.rows] == [
        str(i) for i in range(8 - count, 8)
    ]
    assert document.table.columns == (
        "轮次",
        "时间",
        "输入",
        "输出",
        "缓存读取",
        "合计",
    )
    assert document.table.rows[-1][2:] == ("770", "7", "70", "777")
    assert not document.blocks and "cid" not in document.summary
    assert len(event.sent_chains) == len(pages)
    for chain, page in zip(event.sent_chains, pages):
        assert len(chain.chain) == 1 and isinstance(chain.chain[0], Image)
        assert base64.b64decode(chain.chain[0].file.removeprefix("base64://")) == page
    assert not env.model_calls and event.is_stopped()


@pytest.mark.parametrize("arguments", ["0", "101", "2 extra", "bad"])
async def test_ctx_picture_invalid_arguments_do_not_query_or_render(env, arguments):
    env.plugin.picture_renderer.render = AsyncMock()
    event = env.event(f"/ctx-pic {arguments}", admin=False)
    await env.scheduler.execute(event)
    assert "/ctx-pic [轮数]" in "".join(event.sent)
    env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()
    env.plugin.picture_renderer.render.assert_not_awaited()


async def test_ctx_picture_empty_and_wrong_owner_remain_text(usage_env):
    env = usage_env
    env.plugin.picture_renderer.render = AsyncMock()
    event = env.event()
    await env.invoke("ctx_pic", event, "")
    assert "上下文为空" in "".join(event.sent)
    env.plugin.picture_renderer.render.assert_not_awaited()
    await env.turn(1)
    env.conversation.user_id = "someone-else"
    event = env.event()
    await env.invoke("ctx_pic", event, "")
    assert "归属" in "".join(event.sent)
    env.plugin.picture_renderer.render.assert_not_awaited()


async def test_ctx_picture_can_show_existing_total_without_invented_round(usage_env):
    env = usage_env
    env.conversation.history = '[{"role":"user","content":"old"}]'
    env.conversation.token_usage = 456
    env.plugin.picture_renderer.render = AsyncMock(return_value=[b"png"])
    event = env.event()
    await env.invoke("ctx_pic", event, "")
    document = env.plugin.picture_renderer.render.call_args.args[0]
    assert "无明细" in document.summary
    assert document.table.columns == ("轮次", "时间", "合计")
    assert document.table.rows == (("—", "—", "456"),)


def test_ctx_table_distinguishes_missing_values_and_explicit_zero():
    records = [
        {"round": i, "time": "2026-09-23T15:10:36+08:00", "values": values}
        for i, values in enumerate([{"input": 100, "cached": 0}, {"input": 200}, {}], 1)
    ]
    document = usage_picture(records)
    assert document.table.columns == ("轮次", "时间", "输入", "缓存读取")
    assert document.table.rows[0][2:] == ("100", "0")
    assert document.table.rows[1][2:] == ("200", "—")
    assert document.table.rows[2][2:] == ("—", "—")
    assert "— 未提供" in document.summary
    unknown = usage_picture(records[-1:])
    assert unknown.table.columns == ("轮次", "时间", "用量")
    assert unknown.table.rows[0][2] == "未提供用量"
