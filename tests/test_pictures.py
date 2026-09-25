"""Image commands share query rules and render sanitized data through local HTML."""

import base64
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image
from astrbot_plugin_dev_helper.pictures import (
    LINE_HEIGHT,
    PAGE_LINES,
    TABLE_PAGE_ROWS,
    WIDTH,
    LocalPictureRenderer,
    PictureBlock,
    PictureDocument,
    PictureTable,
    RenderError,
    build_html,
)
from PIL import Image as PILImage


def png_bytes(color):
    output = io.BytesIO()
    PILImage.new("RGB", (10, 10), color).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(
    "plugin_config",
    [None, {"chatlog_default_count": 3, "logs_default_count": 4}],
    indirect=True,
)
@pytest.mark.parametrize(
    "command,count,warning",
    [
        ("/logs-pic", 30, False),
        ("/logs-pic warning", 30, True),
        ("/logs-pic warning 2", 2, True),
        ("/chatlog-pic", 10, False),
        ("/chatlog-pic 2", 2, False),
    ],
)
async def test_picture_commands_query_redact_and_send_pages(
    env, command, count, warning
):
    pages = [png_bytes("red"), png_bytes("blue")]
    env.plugin.picture_renderer.render = AsyncMock(return_value=pages)
    is_chatlog = "chatlog" in command
    if not command.split()[-1].isdigit():
        count = env.plugin_config[
            "chatlog_default_count" if is_chatlog else "logs_default_count"
        ]
    event = env.event(command, group="group" if is_chatlog else None, admin=False)
    env.broker.log_cache.clear()
    for i in range(70):
        env.broker.publish(
            {
                "level": "WARNING" if i % 2 else "INFO",
                "time": i,
                "data": f"line-{i} api_key=secret-log-{i}",
            }
        )
    manager = env.context.conversation_manager
    manager.get_curr_conversation_id.return_value = "cid"
    manager.get_conversation.return_value = SimpleNamespace(
        user_id=event.unified_msg_origin,
        platform_id="qq_one",
        history=json.dumps(
            [
                {
                    "role": "tool",
                    "content": '{"password":"secret-chat"}',
                    "tool_call_id": f"call-{i}",
                }
                for i in range(15)
            ]
        ),
    )

    await env.scheduler.execute(event)

    document = env.plugin.picture_renderer.render.call_args.args[0]
    if is_chatlog:
        records = json.loads(document.blocks[0].text)
        assert len(records) == count
        assert records[0]["tool_call_id"] == f"call-{15 - count}"
        assert records[-1]["tool_call_id"] == "call-14"
        assert records[-1]["role"] == "tool"
        assert json.loads(records[-1]["content"]) == {"password": "[已隐藏]"}
        assert event.unified_msg_origin in document.summary
    else:
        indices = [i for i in range(70) if not warning or i % 2][-count:]
        assert [block.text for block in document.blocks] == [
            f"line-{i} api_key=[已隐藏]" for i in indices
        ]
        assert [block.level for block in document.blocks] == [
            "WARNING" if i % 2 else "INFO" for i in indices
        ]
    assert env.plugin.picture_renderer.render.await_count == 1
    assert len(event.sent_chains) == 2
    for chain, expected in zip(event.sent_chains, pages):
        assert len(chain.chain) == 1 and isinstance(chain.chain[0], Image)
        assert (
            base64.b64decode(chain.chain[0].file.removeprefix("base64://")) == expected
        )
    assert event.is_stopped() and event.call_llm is False and not env.model_calls


@pytest.mark.parametrize(
    "command,group,message",
    [
        ("/logs-pic", "group", "私聊"),
        ("/logs-pic 0", None, "参数"),
        ("/logs-pic warning 101", None, "参数"),
        ("/logs-pic nope", None, "参数"),
        ("/chatlog-pic extra 1", None, "/chatlog-pic"),
        ("/chatlog-pic", None, "没有选中的对话"),
    ],
)
async def test_picture_command_errors_do_not_render(env, command, group, message):
    env.plugin.picture_renderer.render = AsyncMock(
        side_effect=AssertionError("Unexpected render")
    )
    event = env.event(command, group=group, admin=False)
    await env.scheduler.execute(event)
    assert message in "".join(event.sent)
    env.plugin.picture_renderer.render.assert_not_awaited()
    manager = env.context.conversation_manager
    manager.get_conversation.assert_not_awaited()
    assert not env.model_calls


async def test_picture_chatlog_rejects_other_session_before_rendering(env):
    env.plugin.picture_renderer.render = AsyncMock()
    manager = env.context.conversation_manager
    manager.get_curr_conversation_id.return_value = "cid"
    manager.get_conversation.return_value = SimpleNamespace(
        user_id="other", platform_id="qq_one"
    )
    event = env.event("/chatlog-pic", admin=False)
    await env.scheduler.execute(event)
    assert "归属" in "".join(event.sent)
    env.plugin.picture_renderer.render.assert_not_awaited()


async def test_render_error_is_explicit_and_does_not_send_raw_records(env):
    env.plugin.picture_renderer.render = AsyncMock(
        side_effect=RenderError("无法启动本地 Chromium")
    )
    env.broker.log_cache.clear()
    env.broker.publish({"level": "INFO", "time": 0, "data": "diagnostic-only"})
    event = env.event("/logs-pic", admin=False)
    await env.scheduler.execute(event)
    assert "无法启动本地 Chromium" in "".join(event.sent)
    assert "diagnostic-only" not in "".join(event.sent)
    assert event.send.await_count == 1 and not env.model_calls


@pytest.mark.parametrize(
    "error,message", [(ImportError(), "依赖未安装"), (TimeoutError(), "渲染超时")]
)
async def test_local_renderer_reports_dependency_and_timeout_errors(
    monkeypatch, error, message
):
    renderer = LocalPictureRenderer(lambda: None)
    monkeypatch.setattr(renderer, "_render", AsyncMock(side_effect=error))
    with pytest.raises(RenderError, match=message):
        await renderer.render(PictureDocument("test", "", ()))


def test_html_escapes_logs_and_highlights_json_after_redaction():
    html = build_html(
        PictureDocument(
            "<标题>",
            "password=header-secret",
            (
                PictureBlock(
                    '<img src="https://invalid.example/x">\napi_key=log-secret',
                    "log",
                    "WARNING",
                ),
                PictureBlock(
                    json.dumps(
                        {
                            "key": "内容",
                            "password": "json-secret",
                            "count": 3,
                            "enabled": True,
                            "empty": None,
                        }
                    ),
                    "json",
                ),
            ),
        )
    )
    assert "<img src=" not in html and "&lt;img src=" in html
    assert "<标题>" not in html and "&lt;标题&gt;" in html
    assert all(
        secret not in html for secret in ("header-secret", "log-secret", "json-secret")
    )
    assert "#d4b95e" in html
    assert 'class="nt"' in html and 'class="kc"' in html
    assert "[已隐藏]" in html
    assert '<pre class="json"><span class="p">{</span>\n' in html


@pytest.mark.skipif(
    os.environ.get("PICTURE_TESTS") != "1",
    reason="Set PICTURE_TESTS=1 with the selected browsers installed",
)
async def test_real_browser_paginates_without_lost_lines_or_network_requests():
    from playwright.async_api import async_playwright

    executable_path = os.environ.get("ASTRBOT_BROWSER_EXECUTABLE", "")
    text = "\n".join(
        f"记录 {i:03d} <img src='https://invalid.example/{i}'>" for i in range(130)
    )
    document = PictureDocument(
        "日志分页检查", "本地渲染", (PictureBlock(text, "log", "INFO"),)
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, executable_path=executable_path or None
        )
        try:
            page = await browser.new_page(viewport={"width": WIDTH, "height": 2100})
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(build_html(document))
            assert await page.locator("#content").inner_text() == text
            assert (
                await page.locator("#content").evaluate("el => el.scrollHeight")
                == 130 * LINE_HEIGHT
            )
            assert await page.locator("img").count() == 0
            assert requests == []
            # Long JSON string values must wrap without losing characters or
            # introducing fractional line heights that cut through the next page.
            value = {"content": "中文 and ASCII long line " * 700, "enabled": True}
            wrapped = PictureDocument(
                "JSON 分页", "", (PictureBlock(json.dumps(value), "json"),)
            )
            await page.set_content(build_html(wrapped))
            assert json.loads(await page.locator("#content").inner_text()) == value
            metrics = await page.locator("#content").evaluate(
                "el => ({height: el.scrollHeight, width: el.scrollWidth, client: el.clientWidth})"
            )
            assert metrics["height"] > PAGE_LINES * LINE_HEIGHT
            assert metrics["height"] % LINE_HEIGHT == 0
            assert metrics["width"] == metrics["client"]
            assert requests == []
        finally:
            await browser.close()
    from astrbot_plugin_browser.service import BrowserService

    service = BrowserService(browser_executable=executable_path or "")
    await service.initialize()
    try:
        images = await LocalPictureRenderer(lambda: service).render(document)
    finally:
        await service.close()
    assert len(images) == 3
    sizes = [PILImage.open(io.BytesIO(image)).size for image in images]
    assert all(width == WIDTH for width, height in sizes)
    assert sizes[0] == sizes[1]
    assert (
        sizes[0][1] - sizes[2][1] == (PAGE_LINES - (130 - PAGE_LINES * 2)) * LINE_HEIGHT
    )


async def test_missing_shared_browser_service_has_actionable_error():
    with pytest.raises(RenderError, match="请启用 astrbot_plugin_browser"):
        await LocalPictureRenderer(lambda: None).render(PictureDocument("test", "", ()))


@pytest.mark.skipif(
    os.environ.get("PICTURE_TESTS") != "1", reason="Requires installed browser"
)
async def test_usage_table_pages_keep_complete_rows_and_repeat_headers(monkeypatch):
    from playwright.async_api import Locator

    executable_path = os.environ.get("ASTRBOT_BROWSER_EXECUTABLE", "")
    columns = (
        "轮次",
        "时间",
        "输入",
        "输出",
        "缓存读取",
        "缓存写入",
        "非缓存输入",
        "思考",
        "合计",
    )
    rows = tuple(
        (
            str(i),
            "09-23 15:10:36",
            "106,177",
            "653",
            "0",
            "—",
            "106,177",
            "100",
            "106,830",
        )
        for i in range(1, 2 * TABLE_PAGE_ROWS + 2)
    )
    document = PictureDocument("上下文用量", "tokens", (), PictureTable(columns, rows))
    captured = []
    screenshot = Locator.screenshot

    async def capture(locator, *args, **kwargs):
        headers = await locator.locator("th").all_text_contents()
        cells = await locator.locator("tbody tr").evaluate_all(
            "rows => rows.map(row => [...row.cells].map(cell => cell.textContent))"
        )
        footer = await locator.locator("footer").inner_text()
        assert await locator.locator("#content").evaluate(
            "el => el.scrollWidth === el.clientWidth"
        )
        assert await locator.locator("tbody tr").evaluate_all(
            "rows => rows.every(row => [...row.cells].every(cell => cell.scrollWidth <= cell.clientWidth))"
        )
        captured.append((headers, cells, footer))
        return await screenshot(locator, *args, **kwargs)

    monkeypatch.setattr(Locator, "screenshot", capture)
    from astrbot_plugin_browser.service import BrowserService

    service = BrowserService(browser_executable=executable_path or "")
    await service.initialize()
    try:
        images = await LocalPictureRenderer(lambda: service).render(document)
    finally:
        await service.close()
    assert len(images) == 3
    assert [len(page[1]) for page in captured] == [TABLE_PAGE_ROWS, TABLE_PAGE_ROWS, 1]
    assert tuple(tuple(row) for page in captured for row in page[1]) == rows
    for index, (headers, _, footer) in enumerate(captured, 1):
        assert headers == list(columns)
        assert footer == f"开发助手 · 第 {index} / 3 页"
    sizes = [PILImage.open(io.BytesIO(data)).size for data in images]
    assert all(width == WIDTH for width, _ in sizes)
    assert sizes[0] == sizes[1] and sizes[2][1] < sizes[0][1]
