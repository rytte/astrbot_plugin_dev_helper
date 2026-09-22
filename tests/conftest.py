"""Exercise the plugin with real AstrBot event, registry and pipeline classes."""

import copy
import functools
import importlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ASTRBOT_SOURCE = Path(os.environ.get("ASTRBOT_SOURCE", PLUGIN_DIR.parent / "AstrBot"))
sys.path.insert(0, str(ASTRBOT_SOURCE))
sys.path.insert(0, str(PLUGIN_DIR.parent))
os.environ["ASTRBOT_ROOT"] = tempfile.mkdtemp(prefix="dev-helper-tests-")

from astrbot.api.event import AstrMessageEvent  # noqa: E402
from astrbot.api.message_components import Node, Plain  # noqa: E402
from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402
from astrbot.core.config.default import DEFAULT_CONFIG  # noqa: E402
from astrbot.core.log import LogBroker, LogQueueHandler  # noqa: E402
from astrbot.core.pipeline.context import PipelineContext, call_handler  # noqa: E402
from astrbot.core.pipeline.process_stage.stage import (  # noqa: E402
    ProcessStage,
    StarRequestSubStage,
)
from astrbot.core.pipeline.respond.stage import RespondStage  # noqa: E402
from astrbot.core.pipeline.result_decorate.stage import (  # noqa: E402
    ResultDecorateStage,
)
from astrbot.core.pipeline.scheduler import PipelineScheduler  # noqa: E402
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage  # noqa: E402
from astrbot.core.platform.astrbot_message import (  # noqa: E402
    AstrBotMessage,
    MessageMember,
)
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402
from astrbot.core.star.star import StarMetadata  # noqa: E402
from astrbot.core.star.star_handler import (  # noqa: E402
    StarHandlerRegistry,
    star_handlers_registry,
)

main = importlib.import_module("astrbot_plugin_dev_helper.main")
catalog = importlib.import_module("astrbot_plugin_dev_helper.catalog")
ORIGINAL_HANDLERS = [
    handler
    for handler in star_handlers_registry
    if handler.handler_module_path == main.__name__
]


@pytest.fixture
def plugin_config(tmp_path, request):
    """Load plugin defaults and saved settings through AstrBot's schema loader."""
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    config_path = tmp_path / "dev_helper_config.json"
    saved = getattr(request, "param", None)
    if saved is not None:
        config_path.write_text(json.dumps(saved), encoding="utf-8")
    return AstrBotConfig(config_path=str(config_path), schema=schema)


@pytest.fixture
async def env(monkeypatch, plugin_config):
    """Build isolated metadata and read-only services without network access."""
    registry = StarHandlerRegistry()
    metadata = StarMetadata(
        name="astrbot_plugin_dev_helper", module_path=main.__name__, activated=True
    )
    owners = {main.__name__: metadata}
    for module_name in (
        "astrbot_plugin_dev_helper.catalog",
        "astrbot.core.star.star_handler",
        "astrbot.core.star.register.star_handler",
        "astrbot.core.pipeline.waking_check.stage",
        "astrbot.core.pipeline.result_decorate.stage",
        "astrbot.core.pipeline.context_utils",
    ):
        monkeypatch.setattr(
            importlib.import_module(module_name), "star_handlers_registry", registry
        )
    for module_name in (
        "astrbot_plugin_dev_helper.catalog",
        "astrbot.core.star.star_handler",
        "astrbot.core.pipeline.waking_check.stage",
        "astrbot.core.pipeline.context_utils",
        "astrbot.core.pipeline.process_stage.method.star_request",
        "astrbot.core.pipeline.result_decorate.stage",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "star_map", owners)
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["admins_id"] = ["admin"]
    config["wake_prefix"] = ["/"]
    config["plugin_set"] = ["*"]
    config["provider_settings"]["enable"] = True
    manager = SimpleNamespace(func_list=[], iter_builtin_tools=lambda: [])
    context = Context.__new__(Context)
    context.provider_manager = SimpleNamespace(llm_tools=manager)
    context.get_config = lambda umo=None: config
    context.get_all_stars = lambda: list(owners.values())
    context.get_using_tts_provider = lambda umo=None: None
    context.get_using_tts_provider_async = AsyncMock(return_value=None)
    context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value=None),
        get_conversation=AsyncMock(),
    )
    plugin = main.Main(context, plugin_config)
    metadata.star_cls = plugin
    for original in ORIGINAL_HANDLERS:
        handler = copy.copy(original)
        handler.handler = functools.partial(original.handler, plugin)
        registry.append(handler)
    await plugin.initialize()

    def event(
        text="",
        user="admin",
        platform="qq_one",
        group=None,
        admin=True,
        platform_name="aiocqhttp",
    ):
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        )
        message.self_id = "robot"
        message.session_id = group or user
        message.message_id = "message"
        message.sender = MessageMember(user_id=user, nickname=user)
        message.group_id = group
        message.message_str = text
        message.message = [Plain(text)]
        result = AstrMessageEvent(
            text,
            message,
            PlatformMetadata(platform_name, "Test", platform),
            message.session_id,
        )
        result.role = "admin" if admin else "member"
        result.sent = []
        result.sent_chains = []

        def text_content(components):
            return "".join(
                text_content(comp.content) if isinstance(comp, Node) else comp.text
                for comp in components
                if isinstance(comp, (Node, Plain))
            )

        async def send(chain):
            result.sent.append(text_content(chain.chain))
            result.sent_chains.append(chain)
            result._has_send_oper = True

        result.send = AsyncMock(side_effect=send)
        return result

    broker = LogBroker()
    log_handler = LogQueueHandler(broker)
    monkeypatch.setattr(logging.getLogger("astrbot"), "handlers", [log_handler])
    pipeline_context = PipelineContext(
        config, SimpleNamespace(context=context), "default"
    )
    waking = WakingCheckStage()
    await waking.initialize(pipeline_context)
    session_class = importlib.import_module(
        "astrbot.core.star.session_plugin_manager"
    ).SessionPluginManager
    monkeypatch.setattr(
        session_class,
        "filter_handlers_by_session",
        AsyncMock(side_effect=lambda event, handlers: handlers),
    )

    model_calls = []

    class FakeAgent:
        async def process(self, event):
            model_calls.append(event)
            yield None

    request = StarRequestSubStage()
    await request.initialize(pipeline_context)
    process = ProcessStage()
    process.ctx = pipeline_context
    process.star_request_sub_stage = request
    process.agent_sub_stage = FakeAgent()

    decorate = ResultDecorateStage()
    await decorate.initialize(pipeline_context)
    respond = RespondStage()
    await respond.initialize(pipeline_context)
    scheduler = PipelineScheduler.__new__(PipelineScheduler)
    scheduler.ctx = pipeline_context
    scheduler.stages = [waking, process, decorate, respond]
    response_scheduler = PipelineScheduler.__new__(PipelineScheduler)
    response_scheduler.ctx = pipeline_context
    response_scheduler.stages = [decorate, respond]

    async def invoke(method, event, arguments):
        """Invoke a handler directly, letting AstrBot deliver each yielded result."""
        async for _ in call_handler(event, getattr(plugin, method), arguments):
            await response_scheduler.execute(event)

    return SimpleNamespace(
        plugin=plugin,
        plugin_config=plugin_config,
        context=context,
        registry=registry,
        owners=owners,
        config=config,
        manager=manager,
        event=event,
        broker=broker,
        scheduler=scheduler,
        decorate=decorate,
        invoke=invoke,
        waking=waking,
        main=main,
        catalog=catalog,
        model_calls=model_calls,
    )
