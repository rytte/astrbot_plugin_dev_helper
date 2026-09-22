"""Verify configured defaults, explicit overrides and invalid configuration."""

import json
import re
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "plugin_config,chatlog_count,logs_count",
    [
        (None, 10, 30),
        ({"chatlog_default_count": 2, "logs_default_count": 3}, 2, 3),
        ({"chatlog_default_count": 1, "logs_default_count": 100}, 1, 100),
        ({"chatlog_default_count": 100, "logs_default_count": 1}, 100, 1),
    ],
    indirect=["plugin_config"],
)
@pytest.mark.parametrize(
    "method,arguments", [("chatlog", ""), ("logs", ""), ("logs", "warning")]
)
async def test_commands_use_saved_defaults_and_allow_explicit_overrides(
    env, method, arguments, chatlog_count, logs_count
):
    event = env.event()
    if method == "chatlog":
        indices = list(range(110))
        manager = env.context.conversation_manager
        manager.get_curr_conversation_id.return_value = "cid"
        manager.get_conversation.return_value = SimpleNamespace(
            user_id=event.unified_msg_origin,
            platform_id="qq_one",
            history=json.dumps(
                [{"role": "user", "content": f"entry[{i}]"} for i in indices]
            ),
        )
        default_count = chatlog_count
    else:
        records = [
            {
                "level": "WARNING" if i % 2 else "INFO",
                "time": 1000 + i,
                "data": f"entry[{i}]",
            }
            for i in range(220)
        ]
        indices = [i for i in range(220) if not arguments or i % 2]
        default_count = logs_count

    for suffix, count in (("", default_count), ("5", 5)):
        if method == "logs":
            # Standard delivery logs its own reply; start each query with the same data.
            env.broker.log_cache.clear()
            env.broker.log_cache.extend(records)
        event = env.event()
        await env.invoke(method, event, f"{arguments} {suffix}".strip())
        text = "".join(event.sent)
        assert "显示最近 " + str(count) + " 条" in text
        assert [int(i) for i in re.findall(r"entry\[(\d+)\]", text)] == indices[-count:]


@pytest.mark.parametrize("field", ["chatlog_default_count", "logs_default_count"])
@pytest.mark.parametrize("value", [True, False, "10", 1.5, 0, -1, 101, None])
def test_invalid_config_values_fail_with_field_and_range(env, field, value):
    config = dict(env.plugin_config)
    config[field] = value
    with pytest.raises(ValueError, match=field + " 必须是 1–100 的整数"):
        env.main.Main(env.context, config)


@pytest.mark.parametrize(
    "config,message",
    [
        (None, "配置必须是对象"),
        ([], "配置必须是对象"),
        ({}, "缺少配置项"),
        ({"chatlog_default_count": 20}, "缺少配置项：logs_default_count"),
        (
            {"chatlog_default_count": 20, "logs_default_count": 30, "unknown": 10},
            "未知配置项，请移除：unknown",
        ),
    ],
)
def test_invalid_config_shape_is_rejected(env, config, message):
    with pytest.raises(ValueError, match=message):
        env.main.Main(env.context, config)
