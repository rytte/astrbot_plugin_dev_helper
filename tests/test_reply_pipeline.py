"""Exercise standard reply decoration and delivery without a platform connection."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Node, Plain
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.pipeline.result_decorate import stage as decorate_module
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata


@pytest.mark.parametrize("command", ["/inspect tools", "/logs 1", "/chatlog 1"])
@pytest.mark.parametrize(
    "platform_name,threshold,forwarded",
    [("aiocqhttp", 1500, True), ("aiocqhttp", 40000, False), ("telegram", 1500, False)],
)
async def test_long_replies_follow_core_forward_settings(
    env, monkeypatch, command, platform_name, threshold, forwarded
):
    env.config["platform_settings"]["forward_threshold"] = threshold
    env.config["t2i"] = True
    await env.decorate.initialize(env.scheduler.ctx)
    renderer = AsyncMock(side_effect=AssertionError("Unexpected text-to-image request"))
    monkeypatch.setattr(decorate_module.html_renderer, "render_t2i", renderer)
    payload = "visible " * 2200 + "api_key=reply-secret"
    env.manager.func_list = [
        FunctionTool(name=f"tool_{i}", description="visible " * 250, parameters={})
        for i in range(12)
    ]
    env.broker.log_cache.clear()
    env.broker.publish({"level": "INFO", "time": 1000, "data": payload})
    event = env.event(command, admin=False, platform_name=platform_name)
    manager = env.context.conversation_manager
    manager.get_curr_conversation_id.return_value = "cid"
    manager.get_conversation.return_value = SimpleNamespace(
        user_id=event.unified_msg_origin,
        platform_id="qq_one",
        history=json.dumps([{"role": "user", "content": payload}]),
    )

    await env.scheduler.execute(event)

    event.send.assert_awaited_once()
    chain = event.sent_chains[0]
    assert len(chain.chain) == 1
    assert isinstance(chain.chain[0], Node if forwarded else Plain)
    text = "".join(event.sent)
    assert len(text) > 14000
    assert "visible" in text and "reply-secret" not in text
    if command == "/inspect tools":
        assert all(f"tool_{i}" in text for i in range(12))
        assert text.count("visible " * 250) == 12
    else:
        assert "[已隐藏]" in text
    assert event.is_stopped() and event.call_llm is False
    assert not env.model_calls
    renderer.assert_not_awaited()


async def test_response_is_sent_before_stopping_later_handlers(env):
    calls = []

    async def observer(event):
        calls.append(event)

    env.registry.append(
        StarHandlerMetadata(
            EventType.AdapterMessageEvent,
            "observer_on_message",
            "on_message",
            "observer.main",
            observer,
            [],
        )
    )
    env.owners["observer.main"] = StarMetadata(name="observer", activated=True)
    event = env.event("/inspect", admin=False)

    await env.scheduler.execute(event)

    assert "开发助手" in "".join(event.sent)
    event.send.assert_awaited_once()
    assert event.is_stopped()
    assert not calls and not env.model_calls
