"""Render diagnostic text and redact recognizable credentials."""

from __future__ import annotations

import json
import re
from typing import Any

HIDDEN = "[已隐藏]"
SECRET_NAMES = (
    r"(?:[a-z0-9]+[_.-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"token|secret|client[_-]?secret|password|passwd|authorization|"
    r"proxy-authorization|cookie|set-cookie|密钥|令牌|密码)"
)
SECRET_KEY = re.compile(rf"^{SECRET_NAMES}$", re.I)
SECRET_VALUE = re.compile(
    rf"(?P<prefix>\b{SECRET_NAMES}[\"']?\s*[:=]\s*)"
    "(?:"
    + re.escape(HIDDEN)
    + r"(?=$|[\s,;}\]])|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)",
    re.I,
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
JSON_START = re.compile(r'[\[{"]')
JSON_DECODER = json.JSONDecoder()
SECRET_HEADER = re.compile(
    r"(?im)\b(?:proxy-authorization|authorization|set-cookie|cookie)\s*:\s*[^\r\n]+"
)
MAX_REDACTION_DEPTH = 32
DEEP_HIDDEN = "[嵌套过深，内容已隐藏]"


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Redact credentials in data, serialized JSON and surrounding plain text.

    Args:
        value: Text or JSON-compatible diagnostic data.
        _depth: Internal recursion depth, including serialized JSON layers.

    Returns:
        Sanitized copy, preserving ordinary JSON structure.
    """
    if _depth >= MAX_REDACTION_DEPTH:
        return DEEP_HIDDEN
    if isinstance(value, dict):
        return {
            key: HIDDEN
            if SECRET_KEY.fullmatch(str(key))
            else redact(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value]
    if not isinstance(value, str):
        return value
    text = ANSI.sub("", value)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    stripped = text.strip()
    if stripped.startswith(("{", "[", '"')):
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        except RecursionError:
            return DEEP_HIDDEN
        else:
            cleaned = redact(decoded, _depth=_depth + 1)
            return (
                text if cleaned == decoded else json.dumps(cleaned, ensure_ascii=False)
            )

    # Logs may contain several JSON values after a timestamp or message prefix.
    # Keep free-text credentials intact until the plain-text pass, so an embedded
    # object inside password="..." cannot split the sensitive assignment.
    protected = sorted(
        (match.start(), match.end())
        for pattern in (SECRET_VALUE, SECRET_HEADER)
        for match in pattern.finditer(text)
    )
    protected_index = 0
    parts = []
    cursor = 0
    for candidate in JSON_START.finditer(text):
        start = candidate.start()
        if start < cursor:
            continue
        while (
            protected_index < len(protected) and protected[protected_index][1] <= start
        ):
            protected_index += 1
        if protected_index < len(protected) and protected[protected_index][0] <= start:
            continue
        try:
            decoded, end = JSON_DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        except RecursionError:
            return DEEP_HIDDEN
        if isinstance(decoded, str) and not decoded.lstrip().startswith(
            ("{", "[", '"')
        ):
            continue
        cleaned = redact(decoded, _depth=_depth + 1)
        parts.append(_redact_plain_text(text[cursor:start]))
        parts.append(
            text[start:end]
            if cleaned == decoded
            else json.dumps(cleaned, ensure_ascii=False)
        )
        cursor = end
    parts.append(_redact_plain_text(text[cursor:]))
    return "".join(parts)


def _redact_plain_text(text: str) -> str:
    """Sanitize text outside parsed JSON without changing JSON string escaping.

    Args:
        text: Plain text with ANSI and control characters already removed.

    Returns:
        Text with recognizable credentials and inline media hidden.
    """
    text = re.sub(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        HIDDEN,
        text,
        flags=re.S,
    )
    text = re.sub(
        r"\bBearer\s+[A-Za-z0-9._~+/=-]+", f"Bearer {HIDDEN}", text, flags=re.I
    )
    text = SECRET_HEADER.sub(HIDDEN, text)
    text = SECRET_VALUE.sub(lambda match: match["prefix"] + HIDDEN, text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", HIDDEN, text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", rf"\1{HIDDEN}@", text)
    text = re.sub(
        r"([?&](?:key|api_key|apiKey|token|access_token|secret|password)=)[^&#\s]+",
        lambda match: match[1] + HIDDEN,
        text,
        flags=re.I,
    )
    return re.sub(r"data:[^\s;,]+;base64,[A-Za-z0-9+/=]+", "[内嵌媒体数据]", text)


def history_text(record: dict) -> str:
    """Render a stored message, including tool calls and media summaries.

    Args:
        record: One saved conversation history entry.

    Returns:
        Text retaining message roles and available diagnostic fields.
    """
    cleaned = redact(record)
    content = cleaned.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {
                "image_url",
                "input_image",
                "image",
                "audio",
                "input_audio",
                "video",
                "video_url",
                "file",
                "input_file",
            }:
                parts.append({"type": block["type"], "summary": "[多媒体内容]"})
            else:
                parts.append(block)
        cleaned["content"] = parts
    return json.dumps(cleaned, ensure_ascii=False, indent=2)


def positive_number(text: str, default: int, maximum: int | None = None) -> int:
    """Parse an optional positive integer without silently coercing input.

    Args:
        text: Position argument, or an empty string for the documented default.
        default: Value used when the argument is absent.
        maximum: Inclusive upper bound, when applicable.

    Returns:
        Validated integer.

    Raises:
        ValueError: The argument is malformed or outside the supported range.
    """
    if not text:
        return default
    if not re.fullmatch(r"[0-9]{1,9}", text):
        raise ValueError("参数必须是正整数。")
    number = int(text)
    if number < 1 or (maximum is not None and number > maximum):
        raise ValueError(
            f"参数必须在 1–{maximum} 之间。" if maximum else "页码必须大于 0。"
        )
    return number
