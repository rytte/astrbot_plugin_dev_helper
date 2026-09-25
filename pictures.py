"""Offline HTML rendering for diagnostic pictures, paginated on text lines."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from html import escape
from typing import Any, Literal

from .display import redact

WIDTH = 1200
LINE_HEIGHT = 28
PAGE_LINES = 56
TABLE_PAGE_ROWS = 20
LOG_COLORS = {
    "DEBUG": ("#6cb6d9", "bold"),
    "INFO": ("#72c4cc", "bold"),
    "WARNING": ("#d4b95e", "bold"),
    "ERROR": ("#d46a6a", "normal"),
    "CRITICAL": ("#e06060", "bold"),
}


@dataclass(frozen=True)
class PictureBlock:
    text: str
    language: Literal["log", "json", "text"] = "text"
    level: str = ""


@dataclass(frozen=True)
class PictureTable:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class PictureDocument:
    title: str
    summary: str
    blocks: tuple[PictureBlock, ...]
    table: PictureTable | None = None


class RenderError(RuntimeError):
    """A render failure that can be explained without exposing diagnostic data."""


def build_html(document: PictureDocument) -> str:
    """Escape and redact all data before inserting it into a self-contained page."""
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import JsonLexer

    formatter = HtmlFormatter(nowrap=True, style="monokai")
    blocks = []
    for block in document.blocks:
        text = redact(block.text)
        if block.language == "json":
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            body = highlight(text, JsonLexer(stripnl=False, ensurenl=False), formatter)
            blocks.append(f'<pre class="json">{body}</pre>')
        elif block.language == "log":
            color, weight = LOG_COLORS.get(block.level, ("#c8c8c8", "normal"))
            blocks.append(
                f'<pre class="log" style="color:{color};font-weight:{weight}"><span>{escape(text)}</span></pre>'
            )
        else:
            blocks.append(f"<pre><span>{escape(text)}</span></pre>")
    if document.table is not None:
        table = document.table
        if any(len(row) != len(table.columns) for row in table.rows):
            raise ValueError("图片表格列数不一致。")
        columns = "".join(
            f"<th>{escape(redact(column))}</th>" for column in table.columns
        )
        rows = "".join(
            "<tr>"
            + "".join(f"<td>{escape(redact(cell))}</td>" for cell in row)
            + "</tr>"
            for row in table.rows
        )
        blocks.append(
            '<table class="usage-table"><colgroup><col class="round-column">'
            '<col class="time-column"></colgroup>'
            f"<thead><tr>{columns}</tr></thead><tbody>{rows}</tbody></table>"
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; font-src 'none'">
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #1e1e1e; color: #c8c8c8; }}
#sheet {{ width: {WIDTH}px; padding: 32px; background: #1e1e1e;
  font-family: "Cascadia Mono", Consolas, "Noto Sans Mono CJK SC", "Microsoft YaHei", monospace; }}
header {{ margin-bottom: 24px; border-bottom: 1px solid #414141; padding-bottom: 20px; }}
h1 {{ margin: 0 0 12px; font-size: 26px; line-height: 36px; color: #eee; }}
.summary {{ font-size: 16px; line-height: 24px; white-space: pre-wrap; overflow-wrap: anywhere; }}
#window {{ overflow: hidden; }}
#content {{ display: flow-root; }}
#content pre {{ margin: 0; padding: 0; border: 0; font-family: inherit; font-size: 18px;
  line-height: {LINE_HEIGHT}px; white-space: pre-wrap; overflow-wrap: anywhere; tab-size: 4; }}
#content pre.log {{ padding-left: 26ch; text-indent: -26ch; }}
pre:empty::before {{ content: "\\00a0"; }}
.usage-table {{ width: 100%; border-collapse: collapse; table-layout: fixed;
  font-size: 16px; line-height: {LINE_HEIGHT}px; font-variant-numeric: tabular-nums; }}
.usage-table .round-column {{ width: 68px; }}
.usage-table .time-column {{ width: 180px; }}
.usage-table th, .usage-table td {{ text-align: right; padding: 14px 12px;
  border-bottom: 1px solid #343c43; overflow-wrap: anywhere; }}
.usage-table th {{ color: #9cdcfe; background: #252d34; font-weight: normal; }}
.usage-table td {{ color: #e3e8ed; }}
.usage-table th:nth-child(-n+2), .usage-table td:nth-child(-n+2) {{ text-align: left; }}
.usage-table tbody tr:nth-child(even) {{ background: #22282d; }}
.usage-table td:first-child {{ color: #72c4cc; }}
footer {{ margin-top: 20px; padding-top: 12px; border-top: 1px solid #414141;
  font-size: 14px; line-height: 20px; color: #888; }}
{formatter.get_style_defs(".json")}
/* VS Code dark JSON token colours. */
.json {{ background: transparent; }}
.json .nt {{ color: #9cdcfe; }}
.json .s, .json .s2 {{ color: #ce9178; }}
.json .mi, .json .mf, .json .m {{ color: #b5cea8; }}
.json .kc {{ color: #569cd6; }}
.json .p {{ color: #d4d4d4; }}
</style></head><body><main id="sheet">
<header><h1>{escape(redact(document.title))}</h1><div class="summary">{escape(redact(document.summary))}</div></header>
<div id="window"><div id="content">{"".join(blocks)}</div></div>
<footer id="footer"></footer></main></body></html>"""


class LocalPictureRenderer:
    """Serialize picture jobs through the shared offline browser service."""

    def __init__(self, browser_service_resolver: Callable[[], Any] | None) -> None:
        self.browser_service_resolver = browser_service_resolver
        self._lock = asyncio.Lock()

    async def render(self, document: PictureDocument) -> list[bytes]:
        async with self._lock:
            try:
                return await asyncio.wait_for(self._render(document), timeout=120)
            except ImportError as error:
                raise RenderError(
                    "图片渲染依赖未安装。请在 AstrBot 的 Python 环境安装插件 requirements.txt。"
                ) from error
            except asyncio.TimeoutError as error:
                raise RenderError(
                    "图片渲染超时（120 秒），请减少查询条数后重试。"
                ) from error
            except RenderError:
                raise
            except Exception as error:
                raise RenderError(
                    "本地图片渲染失败，请检查浏览器运行环境和中文字体。"
                ) from error

    async def _render(self, document: PictureDocument) -> list[bytes]:
        documents = [document]
        if document.table is not None:
            documents = [
                replace(
                    document,
                    table=replace(
                        document.table,
                        rows=document.table.rows[start : start + TABLE_PAGE_ROWS],
                    ),
                )
                for start in range(0, max(1, len(document.table.rows)), TABLE_PAGE_ROWS)
            ]
        html = await asyncio.to_thread(build_html, documents[0])
        service = (
            self.browser_service_resolver()
            if self.browser_service_resolver is not None
            else None
        )
        if service is None:
            raise RenderError("浏览器服务不可用，请启用 astrbot_plugin_browser 插件。")
        async with service.session(
            viewport={"width": WIDTH, "height": 2100},
            javascript_enabled=True,
            timeout=120,
        ) as page:
            await page.set_content(html, wait_until="load")
            await page.evaluate("document.fonts.ready")
            if document.table is not None:
                images = []
                for index, sheet in enumerate(documents):
                    if index:
                        await page.set_content(
                            await asyncio.to_thread(build_html, sheet),
                            wait_until="load",
                        )
                        await page.evaluate("document.fonts.ready")
                    await page.locator("#footer").evaluate(
                        "(el, label) => el.textContent = label",
                        f"开发助手 · 第 {index + 1} / {len(documents)} 页",
                    )
                    images.append(await page.locator("#sheet").screenshot(type="png"))
                return images
            height = await page.locator("#content").evaluate("el => el.scrollHeight")
            page_height = PAGE_LINES * LINE_HEIGHT
            count = max(1, math.ceil(height / page_height))
            images = []
            for index in range(count):
                offset = index * page_height
                await page.evaluate(
                    """({offset, height, label}) => {
                        document.querySelector('#window').style.height = height + 'px';
                        document.querySelector('#content').style.transform = `translateY(-${offset}px)`;
                        document.querySelector('#footer').textContent = label;
                    }""",
                    {
                        "offset": offset,
                        "height": min(page_height, height - offset),
                        "label": f"开发助手 · 第 {index + 1} / {count} 页",
                    },
                )
                images.append(await page.locator("#sheet").screenshot(type="png"))
            return images
