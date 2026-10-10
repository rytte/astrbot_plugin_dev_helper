"""Exercise persona management against AstrBot's real manager and SQLite store."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.db.sqlite import SQLiteDatabase
from astrbot.core.persona_mgr import PersonaManager
from astrbot.core.utils.shared_preferences import SharedPreferences


@pytest.fixture
async def personas(env, tmp_path):
    db = SQLiteDatabase(str(tmp_path / "personas.db"))
    preferences = SharedPreferences(db, str(tmp_path / "preferences.json"))
    acm = SimpleNamespace(
        default_conf=env.config, confs={"default": env.config}, sp=preferences
    )
    manager = PersonaManager(db, acm)
    await manager.initialize()
    env.context.persona_manager = manager

    async def call(action, payload, event=None, **kwargs):
        context = ContextWrapper(
            context=SimpleNamespace(event=event or env.event(), context=env.context)
        )
        return json.loads(
            await env.plugin.persona_tool.call(context, action, payload, **kwargs)
        )

    yield SimpleNamespace(
        env=env,
        db=db,
        sp=preferences,
        acm=acm,
        manager=manager,
        tool=env.plugin.persona_tool,
        call=call,
    )
    await preferences.close()
    await db.engine.dispose()


async def test_tool_registration_discovery_idempotency_and_schema(env):
    tool = env.plugin.persona_tool
    assert env.manager.func_list == [tool]
    await env.plugin.initialize()
    assert env.manager.func_list == [tool]
    assert tool.name == "dev_helper_persona"
    assert tool.handler_module_path == env.main.__name__
    assert "payload" in tool.parameters["properties"]
    parameters = tool.parameters
    jsonschema.Draft202012Validator.check_schema(parameters)
    jsonschema.validate(
        {"action": "update", "payload": {"persona_id": "engineer", "tools": None}},
        parameters,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"action": "get", "payload": {}, "confirm": True}, parameters
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"action": "get", "payload": {"legacy_id": "x"}}, parameters
        )
    tools = ToolSet(tools=[tool])
    assert tools.openai_schema()[0]["function"]["parameters"]["required"] == [
        "action",
        "payload",
    ]
    assert "payload" in tools.anthropic_schema()[0]["input_schema"]["properties"]
    google = tools.google_schema()["function_declarations"][0]["parameters"]
    assert (
        google["properties"]["payload"]["properties"]["tools"]["anyOf"][1]["type"]
        == "null"
    )


async def test_registration_rejects_another_tool_with_the_same_name(env):
    env.manager.func_list = [
        FunctionTool(name="dev_helper_persona", description="other", parameters={})
    ]
    plugin = env.main.Main(env.context, env.plugin_config)
    with pytest.raises(RuntimeError, match="模型工具冲突"):
        await plugin.initialize()
    assert not plugin.ready
    assert len(env.manager.func_list) == 1


@pytest.mark.parametrize(
    "kind,code",
    [
        ("member", "permission_denied"),
        ("group", "private_chat_required"),
        ("api", "permission_denied"),
        ("missing", "permission_denied"),
        ("not_ready", "not_ready"),
    ],
)
async def test_authorization_precedes_argument_parsing_and_reads(
    personas, monkeypatch, kind, code
):
    reader = AsyncMock(wraps=personas.manager.get_persona)
    monkeypatch.setattr(personas.manager, "get_persona", reader)
    event = personas.env.event(
        admin=kind != "member", group="group" if kind == "group" else None
    )
    if kind == "api":
        event.set_extra("_api_key_allow_admin_role", False)
    if kind == "not_ready":
        personas.env.plugin.ready = False
    if kind == "missing":
        result = json.loads(
            await personas.tool.call(ContextWrapper(context=None), ["get"], "invalid")
        )
    else:
        result = await personas.call(["get"], "invalid", event)
    assert result["error"]["code"] == code
    assert not result["ok"]
    reader.assert_not_awaited()
    assert not event.sent


@pytest.mark.parametrize(
    "action,payload",
    [
        ("unknown", {}),
        (None, {}),
        (["get"], {}),
        ("list", None),
        ("get", "engineer"),
        ("list", {"page": True}),
        ("list", {"page": 0}),
        ("list", {"page": "1"}),
        ("list", {"page_size": 101}),
        ("list", {"page_size": 1.5}),
        ("list", {"persona_id": "engineer"}),
        ("get", {}),
        ("get", {"persona_id": 1}),
        ("get", {"persona_id": " "}),
        ("get", {"persona_id": " engineer"}),
        ("get", {"persona_id": "x" * 256}),
        ("get", {"persona_id": "engineer", "system_prompt": "unexpected"}),
        ("create", {"persona_id": "engineer"}),
        ("create", {"persona_id": "engineer", "system_prompt": " "}),
        ("create", {"persona_id": "engineer", "system_prompt": None}),
        ("create", {"persona_id": "engineer", "system_prompt": 1}),
        ("update", {"persona_id": "engineer"}),
        ("update", {"persona_id": "engineer", "begin_dialogs": ["odd"]}),
        ("update", {"persona_id": "engineer", "begin_dialogs": ["user", 1]}),
        ("update", {"persona_id": "engineer", "begin_dialogs": None}),
        ("update", {"persona_id": "engineer", "tools": "lookup"}),
        ("update", {"persona_id": "engineer", "tools": [None]}),
        ("update", {"persona_id": "engineer", "skills": [" "]}),
        ("update", {"persona_id": "engineer", "custom_error_message": True}),
        ("update", {"persona_id": "engineer", "new_persona_id": "renamed"}),
        ("update", {"persona_id": "engineer", "folder_id": "folder"}),
        ("update", {"persona_id": "engineer", "sort_order": 1}),
        ("delete", {"persona_id": "engineer", "confirm": True}),
    ],
)
async def test_invalid_arguments_fail_without_manager_access(
    personas, monkeypatch, action, payload
):
    reader = AsyncMock(wraps=personas.manager.get_persona)
    monkeypatch.setattr(personas.manager, "get_persona", reader)
    result = await personas.call(action, payload)
    assert result["error"]["code"] == "invalid_arguments"
    reader.assert_not_awaited()
    assert await personas.manager.get_all_personas() == []


async def test_unknown_top_level_argument_is_not_silently_ignored(personas):
    result = await personas.call("list", {}, legacy=True)
    assert result["error"]["code"] == "invalid_arguments"


async def test_crud_refreshes_cache_and_survives_manager_reload(personas):
    payload = {
        "persona_id": "engineer",
        "system_prompt": "  Preserve the full prompt.\n",
        "begin_dialogs": ["question", "answer"],
        "tools": ["lookup"],
        "skills": ["coding"],
        "custom_error_message": "Try again.",
    }
    created = await personas.call("create", payload)
    assert (
        created["ok"] and created["data"]["system_prompt"] == payload["system_prompt"]
    )
    assert (
        personas.manager.get_persona_v3_by_id("engineer")["prompt"]
        == payload["system_prompt"]
    )
    detail = await personas.call("get", {"persona_id": "engineer"})
    assert detail["data"] == created["data"]
    updated = await personas.call(
        "update", {"persona_id": "engineer", "system_prompt": "New prompt."}
    )
    for field in (
        "begin_dialogs",
        "tools",
        "skills",
        "custom_error_message",
        "folder_id",
        "sort_order",
    ):
        assert updated["data"][field] == created["data"][field]
    assert personas.manager.get_persona_v3_by_id("engineer")["prompt"] == "New prompt."
    reloaded = PersonaManager(personas.db, personas.acm)
    await reloaded.initialize()
    assert (await reloaded.get_persona("engineer")).system_prompt == "New prompt."
    personas.env.context.persona_manager = reloaded
    deleted = await personas.call("delete", {"persona_id": "engineer"})
    assert deleted["data"] == {"persona_id": "engineer", "deleted": True}
    assert await reloaded.get_all_personas() == []
    assert reloaded.get_persona_v3_by_id("engineer") is None
    await reloaded.initialize()
    assert await reloaded.get_all_personas() == []
    personas.env.context.conversation_manager.get_curr_conversation_id.assert_not_awaited()


async def test_update_preserves_folder_and_sort_order(personas):
    await personas.manager.create_persona(
        "engineer", "Prompt", folder_id="folder", sort_order=17
    )
    result = await personas.call(
        "update", {"persona_id": "engineer", "system_prompt": "Changed"}
    )
    assert result["data"]["folder_id"] == "folder"
    assert result["data"]["sort_order"] == 17


@pytest.mark.parametrize("field", ["tools", "skills"])
async def test_missing_null_and_empty_permission_lists_are_distinct(personas, field):
    await personas.manager.create_persona("engineer", "Prompt", **{field: ["lookup"]})
    omitted = await personas.call(
        "update", {"persona_id": "engineer", "system_prompt": "Changed"}
    )
    assert omitted["data"][field] == ["lookup"]
    for value in ([], None, ["lookup"]):
        result = await personas.call("update", {"persona_id": "engineer", field: value})
        assert result["data"][field] == value
        assert (await personas.manager.get_persona("engineer")).__dict__[field] == value
        assert personas.manager.get_persona_v3_by_id("engineer")[field] == value


async def test_dialogs_and_custom_error_message_can_be_cleared(personas):
    await personas.manager.create_persona(
        "engineer",
        "Prompt",
        begin_dialogs=["user", "assistant"],
        custom_error_message="Oops",
    )
    result = await personas.call(
        "update",
        {"persona_id": "engineer", "begin_dialogs": [], "custom_error_message": None},
    )
    assert result["data"]["begin_dialogs"] == []
    assert result["data"]["custom_error_message"] is None
    assert (
        personas.manager.get_persona_v3_by_id("engineer")["_begin_dialogs_processed"]
        == []
    )


async def test_duplicate_creation_is_not_an_upsert_and_missing_targets_are_errors(
    personas,
):
    await personas.manager.create_persona("engineer", "Original")
    duplicate = await personas.call(
        "create", {"persona_id": "engineer", "system_prompt": "Overwrite"}
    )
    assert duplicate["error"]["code"] == "already_exists"
    assert (await personas.manager.get_persona("engineer")).system_prompt == "Original"
    for action in ("get", "update", "delete"):
        payload = {"persona_id": "missing"}
        if action == "update":
            payload["system_prompt"] = "Changed"
        result = await personas.call(action, payload)
        assert result["error"]["code"] == "not_found"


async def test_list_is_paginated_sorted_and_omits_prompts(personas):
    empty = await personas.call("list", {})
    assert empty["data"] == {"items": [], "total": 0, "page": 1, "page_size": 20}
    for persona_id in ("charlie", "alpha", "bravo"):
        await personas.manager.create_persona(persona_id, "SECRET PROMPT")
    result = await personas.call("list", {"page": 2, "page_size": 2})
    assert result["data"]["items"] == [{"persona_id": "charlie", "folder_id": None}]
    assert result["data"]["total"] == 3
    assert "SECRET" not in json.dumps(result)
    assert (await personas.call("list", {"page": 3, "page_size": 2}))["data"][
        "items"
    ] == []


@pytest.mark.parametrize(
    "kind", ["default_persona", "subagent", "session", "conversation", "cron_job"]
)
async def test_deletion_rejects_native_references_without_unbinding(personas, kind):
    await personas.manager.create_persona("engineer", "Prompt")
    if kind == "default_persona":
        personas.acm.confs["secondary"] = {
            "provider_settings": {"default_personality": "engineer"}
        }
    elif kind == "subagent":
        personas.acm.confs["secondary"] = {
            "subagent_orchestrator": {
                "agents": [{"name": "helper", "persona_id": "engineer"}]
            }
        }
    elif kind == "session":
        await personas.sp.session_put(
            "session-one", "session_service_config", {"persona_id": "engineer"}
        )
    elif kind == "conversation":
        await personas.db.create_conversation(
            "session-one", "qq", persona_id="engineer"
        )
    else:
        await personas.db.create_cron_job(
            "task",
            "active_agent",
            None,
            payload={"persona_id": "engineer"},
            enabled=False,
        )
    result = await personas.call("delete", {"persona_id": "engineer"})
    assert result["error"]["code"] == "persona_in_use"
    details = result["error"]["details"]
    assert details["reference_count"] == 1
    assert details["references"][0]["type"] == kind
    assert (await personas.manager.get_persona("engineer")).system_prompt == "Prompt"
    repeated = await personas.call("delete", {"persona_id": "engineer"})
    assert repeated["error"]["details"] == details


async def test_session_reference_changes_are_visible_without_restart(personas):
    await personas.manager.create_persona("engineer", "Prompt")
    await personas.sp.session_put(
        "session-one", "session_service_config", {"persona_id": "engineer"}
    )
    assert not (await personas.call("delete", {"persona_id": "engineer"}))["ok"]
    await personas.sp.session_put("session-one", "session_service_config", {})
    assert (await personas.call("delete", {"persona_id": "engineer"}))["ok"]


async def test_reference_scan_paginates_without_loading_history(personas, monkeypatch):
    await personas.manager.create_persona("engineer", "Prompt")
    unrelated = [SimpleNamespace(persona_id=None) for _ in range(100)]
    referenced = [
        SimpleNamespace(persona_id="engineer", conversation_id="late-reference")
    ]
    scanner = AsyncMock(side_effect=[(unrelated, 101), (referenced, 101)])
    monkeypatch.setattr(personas.db, "get_filtered_conversations", scanner)
    result = await personas.call("delete", {"persona_id": "engineer"})
    assert result["error"]["code"] == "persona_in_use"
    assert [entry.kwargs for entry in scanner.await_args_list] == [
        {"page": 1, "page_size": 100, "include_history": False},
        {"page": 2, "page_size": 100, "include_history": False},
    ]


async def test_reference_report_is_bounded(personas, monkeypatch):
    await personas.manager.create_persona("engineer", "Prompt")
    references = [
        SimpleNamespace(persona_id="engineer", conversation_id=str(index))
        for index in range(21)
    ]
    monkeypatch.setattr(
        personas.db,
        "get_filtered_conversations",
        AsyncMock(return_value=(references, 21)),
    )
    result = await personas.call("delete", {"persona_id": "engineer"})
    assert result["error"]["details"]["reference_count"] == 21
    assert len(result["error"]["details"]["references"]) == 20


async def test_reference_check_failure_denies_deletion(personas, monkeypatch):
    await personas.manager.create_persona("engineer", "Prompt")
    monkeypatch.setattr(
        personas.db,
        "get_filtered_conversations",
        AsyncMock(side_effect=RuntimeError("storage unavailable")),
    )
    result = await personas.call("delete", {"persona_id": "engineer"})
    assert result["error"]["code"] == "operation_failed"
    assert (await personas.manager.get_persona("engineer")).persona_id == "engineer"


async def test_concurrent_creations_have_one_winner(personas):
    results = await asyncio.gather(
        *[
            personas.call("create", {"persona_id": "engineer", "system_prompt": prompt})
            for prompt in ("First", "Second")
        ]
    )
    assert sum(result["ok"] for result in results) == 1
    assert (
        next(result for result in results if not result["ok"])["error"]["code"]
        == "already_exists"
    )
    assert len(await personas.manager.get_all_personas()) == 1


async def test_standard_tool_executor_returns_json_without_sending_messages(personas):
    event = personas.env.event()
    context = ContextWrapper(
        context=SimpleNamespace(event=event, context=personas.env.context)
    )
    responses = [
        response
        async for response in FunctionToolExecutor.execute(
            tool=personas.tool, run_context=context, action="list", payload={}
        )
    ]
    assert len(responses) == 1
    response = responses[0]
    text = response if isinstance(response, str) else response.content[0].text
    assert json.loads(text)["ok"]
    assert not event.sent


async def test_plugin_termination_disables_a_retained_tool(personas):
    await personas.env.plugin.terminate()
    result = await personas.call("list", {})
    assert result["error"]["code"] == "not_ready"


async def test_subagent_reference_uses_the_core_resolution_semantics(personas):
    await personas.manager.create_persona("engineer", "Prompt")
    personas.acm.confs["secondary"] = {
        "subagent_orchestrator": {
            "agents": [{"name": "helper", "persona_id": " engineer "}]
        }
    }
    result = await personas.call("delete", {"persona_id": "engineer"})
    assert result["error"]["code"] == "persona_in_use"
    assert result["error"]["details"]["references"][0]["type"] == "subagent"


async def test_storage_errors_do_not_log_prompt_or_exception_parameters(
    personas, monkeypatch, caplog
):
    monkeypatch.setattr(
        personas.manager,
        "create_persona",
        AsyncMock(side_effect=RuntimeError("SENSITIVE PROMPT IN DATABASE PARAMETERS")),
    )
    with caplog.at_level(logging.ERROR):
        result = await personas.call(
            "create",
            {"persona_id": "engineer", "system_prompt": "SENSITIVE SYSTEM PROMPT"},
        )
    assert result["error"]["code"] == "operation_failed"
    assert "error_type=RuntimeError" in caplog.text
    assert "SENSITIVE" not in caplog.text
    assert "SENSITIVE" not in json.dumps(result)
