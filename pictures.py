"""Offline HTML rendering for diagnostic pictures, paginated on text lines."""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Literal

from .display import redact

WIDTH = 1200
LINE_HEIGHT = 28
PAGE_LINES = 56
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
class PictureDocument:
    title: str
    summary: str
    blocks: tuple[PictureBlock, ...]


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
    """Serialize local browser jobs; no remote renderer or asset downloads."""

    def __init__(self, executable_path: str) -> None:
        if not isinstance(executable_path, str):
            raise ValueError("开发助手配置 browser_executable 必须是字符串。")
        if executable_path and (
            not Path(executable_path).is_absolute()
            or not Path(executable_path).is_file()
        ):
            raise ValueError(
                "开发助手配置 browser_executable 必须留空或填写已存在的浏览器可执行文件绝对路径，不加引号。"
            )
        self.executable_path = executable_path
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
        from playwright.async_api import Error, async_playwright

        html = await asyncio.to_thread(build_html, document)
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    headless=True,
                    executable_path=self.executable_path or None,
                )
            except Error as error:
                if self.executable_path:
                    raise RenderError(
                        "无法启动 browser_executable 指定的浏览器。请确认该文件是可运行的 Chromium 或 Edge，"
                        "并检查运行权限及系统依赖。"
                    ) from error
                raise RenderError(
                    "无法启动本地 Chromium。请在 AstrBot 的 Python 环境运行 "
                    "python -m playwright install chromium；Linux 还需安装浏览器系统依赖和中文字体，详见插件 README。"
                ) from error
            try:
                page = await browser.new_page(
                    viewport={"width": WIDTH, "height": 2100},
                    device_scale_factor=1,
                    service_workers="block",
                )
                await page.route("**/*", lambda route: route.abort())
                await page.set_content(html, wait_until="load")
                await page.evaluate("document.fonts.ready")
                height = await page.locator("#content").evaluate(
                    "el => el.scrollHeight"
                )
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
            finally:
                await browser.close()
