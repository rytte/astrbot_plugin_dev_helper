"""Regression coverage for nested serialized credentials in diagnostic output."""

import copy
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_dev_helper.display import HIDDEN, redact


@pytest.mark.parametrize("layers", [1, 2, 4])
def test_serialized_json_redacts_secrets_without_changing_value_types(layers):
    value = {
        "password": 'secret with "quotes" and \\slashes',
        "nested": [{"api_key": "secret-key", "token_usage": 42}],
        "message": "普通内容",
    }
    encoded = value
    for _ in range(layers):
        encoded = json.dumps(encoded, ensure_ascii=True)
    cleaned = redact(encoded)
    assert "secret-key" not in cleaned and "secret with" not in cleaned
    assert redact(cleaned) == cleaned
    for _ in range(layers):
        cleaned = json.loads(cleaned)
    assert cleaned == {
        "password": HIDDEN,
        "nested": [{"api_key": HIDDEN, "token_usage": 42}],
        "message": "普通内容",
    }


def test_prefixed_json_keeps_structure_and_redacts_adjacent_plain_credentials():
    payload = json.dumps(
        {"arguments": json.dumps({"password": "nested-secret", "count": 2})}
    )
    text = f'[12:00] response={payload} password="plain-secret" tail'
    cleaned = redact(text)
    assert "nested-secret" not in cleaned and "plain-secret" not in cleaned
    start = cleaned.index("{")
    decoded, _ = json.JSONDecoder().raw_decode(cleaned, start)
    assert json.loads(decoded["arguments"]) == {"password": HIDDEN, "count": 2}
    assert cleaned.startswith("[12:00] response=") and cleaned.endswith(" tail")
    assert redact(cleaned) == cleaned


@pytest.mark.parametrize(
    "text",
    [
        'password="{\\"value\\":\\"plain-secret\\"}"',
        'Authorization: {"value":"plain-secret"}',
        '{"password":"plain-secret", broken JSON',
        'payload="{\\"password\\":\\"plain-secret\\"}"',
    ],
)
def test_json_scanning_does_not_split_sensitive_assignments(text):
    assert "plain-secret" not in redact(text)


def test_redaction_keeps_clean_text_formatting_and_does_not_modify_input():
    text = 'before { "count": 2, "message": "hello" } after [1, 2]'
    assert redact(text) == text
    original = {"role": "tool", "content": '{"password":"sensitive"}'}
    before = copy.deepcopy(original)
    cleaned = redact(original)
    assert original == before
    assert json.loads(cleaned["content"]) == {"password": HIDDEN}


def test_excessive_nesting_is_hidden_instead_of_returning_unsanitized_text():
    text = '{"nested":' * 1100 + '{"password":"deep-secret"}' + "}" * 1100
    cleaned = redact(text)
    assert "deep-secret" not in cleaned and "嵌套过深" in cleaned


@pytest.mark.parametrize("command", ["logs", "chatlog"])
async def test_nested_tool_arguments_are_hidden_in_actual_replies(env, command):
    payload = json.dumps(
        {
            "tool_calls": [
                {
                    "function": {
                        "name": "login",
                        "arguments": json.dumps(
                            {"password": "review-secret-123", "query": "visible"}
                        ),
                    }
                }
            ]
        }
    )
    if command == "logs":
        event = env.event("/logs 1", admin=False)
        env.broker.log_cache.clear()
        env.broker.publish(
            {"level": "WARNING", "time": 1000, "data": "response=" + payload}
        )
    else:
        event = env.event("/chatlog 1", group="group", admin=False)
        env.context.conversation_manager.get_curr_conversation_id.return_value = "cid"
        env.context.conversation_manager.get_conversation.return_value = (
            SimpleNamespace(
                user_id=event.unified_msg_origin,
                platform_id="qq_one",
                history=json.dumps([{"role": "tool", "content": payload}]),
            )
        )
    await env.scheduler.execute(event)
    text = "".join(event.sent)
    assert "review-secret-123" not in text
    assert "visible" in text and HIDDEN in text
    assert not env.model_calls
