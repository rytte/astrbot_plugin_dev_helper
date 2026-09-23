"""Per-turn usage, tied to saved context without modifying conversations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from uuid import uuid4
from weakref import WeakValueDictionary

from .pictures import PictureDocument, PictureTable

MAX_ROUNDS = 100
USAGE_LABELS = {
    "input": "输入",
    "output": "输出",
    "cached": "缓存读取",
    "cache_write": "缓存写入",
    "uncached": "非缓存输入",
    "reasoning": "思考",
    "total": "合计",
}


def field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def count(value):
    return value if type(value) is int and value >= 0 else None


def extract_usage(response) -> dict:
    """Prefer provider reports; never turn an absent field into a reported zero."""
    raw = response.raw_completion
    usage = field(raw, "usage")
    gemini = field(raw, "usage_metadata")
    values = {}
    source = "接口原始用量"
    if gemini is not None:
        mapping = {
            "input": "prompt_token_count",
            "output": "candidates_token_count",
            "cached": "cached_content_token_count",
            "reasoning": "thoughts_token_count",
            "total": "total_token_count",
        }
        values = {key: count(field(gemini, name)) for key, name in mapping.items()}
        # Gemini reports candidates and thinking separately.
        if values["output"] is not None and values["reasoning"] is not None:
            values["output"] += values["reasoning"]
    elif usage is not None:
        if (
            field(usage, "prompt_tokens") is not None
            or field(usage, "completion_tokens") is not None
        ):
            values = {
                "input": count(field(usage, "prompt_tokens")),
                "output": count(field(usage, "completion_tokens")),
                "cached": count(
                    field(field(usage, "prompt_tokens_details"), "cached_tokens")
                ),
                "reasoning": count(
                    field(field(usage, "completion_tokens_details"), "reasoning_tokens")
                ),
            }
        elif field(raw, "type") == "message" or any(
            field(usage, key) is not None
            for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
        ):
            values = {
                "uncached": count(field(usage, "input_tokens")),
                "cached": count(field(usage, "cache_read_input_tokens")),
                "cache_write": count(field(usage, "cache_creation_input_tokens")),
                "output": count(field(usage, "output_tokens")),
            }
            parts = [values[key] for key in ("uncached", "cached", "cache_write")]
            if all(part is not None for part in parts):
                values["input"] = sum(parts)
        else:
            values = {
                "input": count(field(usage, "input_tokens")),
                "output": count(field(usage, "output_tokens")),
                "cached": count(
                    field(field(usage, "input_tokens_details"), "cached_tokens")
                ),
                "reasoning": count(
                    field(field(usage, "output_tokens_details"), "reasoning_tokens")
                ),
            }
        values["total"] = count(field(usage, "total_tokens"))
    else:
        # The normalized object has integer defaults, so all-zero means unknown.
        normalized = response.usage
        parts = {
            "uncached": count(field(normalized, "input_other")),
            "cached": count(field(normalized, "input_cached")),
            "output": count(field(normalized, "output")),
        }
        if any(value for value in parts.values()):
            source = "AstrBot 标准化用量"
            values = parts
            if parts["uncached"] is not None and parts["cached"] is not None:
                values["input"] = parts["uncached"] + parts["cached"]
            # Zero defaults do not establish that those fields were reported.
            values = {key: value for key, value in values.items() if value}
            values.pop("uncached", None)
    values = {key: value for key, value in values.items() if value is not None}
    if "total" not in values and "input" in values and "output" in values:
        values["total"] = values["input"] + values["output"]
    return {"values": values, "source": source if values else "未提供用量"}


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def history_hashes(history: list[dict]) -> list[str]:
    return [
        fingerprint(message)
        for message in history
        if message.get("role") != "_checkpoint"
    ]


def match_records(records: list[dict], hashes: list[str]) -> list[dict]:
    """Match each saved turn once, in order, including repeated identical replies."""
    selected = []
    end = len(hashes)
    for record in reversed(records):
        anchor = record["anchor"]
        for start in range(end - len(anchor), -1, -1):
            if hashes[start : start + len(anchor)] == anchor:
                selected.append(record)
                end = start
                break
    return list(reversed(selected))


class ContextUsage:
    """Store only numbers and hashes via the plugin's KV API, never message text."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        self.pending = WeakValueDictionary()
        self.inflight = WeakValueDictionary()

    def key(self, origin: str, cid: str) -> str:
        return "context_usage:" + fingerprint([origin, cid])

    async def read(self, key: str) -> dict:
        state = await self.plugin.get_kv_data(key, None)
        if state is None:
            return {"generation": uuid4().hex, "next_round": 1, "records": []}
        if not isinstance(state, dict) or set(state) != {
            "generation",
            "next_round",
            "records",
        }:
            raise ValueError("上下文用量记录格式错误。")
        return state

    async def sync(
        self, key: str, history: list[dict], *, force_clear=False
    ) -> tuple[dict, list[dict]]:
        state = await self.read(key)
        matched = match_records(state["records"], history_hashes(history))
        matched_ids = {record["id"] for record in matched}
        for record_id in matched_ids:
            self.pending.pop(record_id, None)
        keep = [
            record
            for record in state["records"]
            if record["id"] in matched_ids
            or (
                not force_clear
                and (task := self.pending.get(record["id"])) is not None
                and not task.done()
            )
        ]
        active = self.inflight.get(key)
        if force_clear or (
            not history and not keep and (active is None or active.done())
        ):
            self.inflight.pop(key, None)
            state = {"generation": uuid4().hex, "next_round": 1, "records": []}
            await self.plugin.delete_kv_data(key)
        elif keep != state["records"]:
            state["records"] = keep
            await self.plugin.put_kv_data(key, state)
        return state, matched

    async def begin(self, origin: str, cid: str, history: list[dict]) -> dict:
        async with self.lock:
            key = self.key(origin, cid)
            # AstrBot serializes main-agent requests per session. An empty
            # history at the start of the next request is a new context.
            state, _ = await self.sync(key, history, force_clear=not history)
            await self.plugin.put_kv_data(key, state)
            self.inflight[key] = asyncio.current_task()
            return {
                "key": key,
                "cid": cid,
                "origin": origin,
                "generation": state["generation"],
                "id": uuid4().hex,
            }

    async def finish(self, ticket: dict, anchor: list[str], response) -> None:
        if not anchor:
            return
        async with self.lock:
            state = await self.read(ticket["key"])
            if state["generation"] != ticket["generation"]:
                return  # A reset invalidated this in-flight request.
            self.inflight.pop(ticket["key"], None)
            if any(record["id"] == ticket["id"] for record in state["records"]):
                return
            record = {
                "id": ticket["id"],
                "anchor": anchor,
                "round": state["next_round"],
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                **extract_usage(response),
            }
            state["next_round"] += 1
            state["records"] = (state["records"] + [record])[-MAX_ROUNDS:]
            self.pending[ticket["id"]] = asyncio.current_task()
            await self.plugin.put_kv_data(ticket["key"], state)


def format_round(record: dict) -> str:
    time = datetime.fromisoformat(record["time"]).strftime("%m-%d %H:%M:%S")
    lines = [f"第 {record['round']} 轮｜{time}"]
    lines.extend(
        f"{label}：{record['values'][key]:,}"
        for key, label in USAGE_LABELS.items()
        if key in record["values"]
    )
    if not record["values"]:
        lines.append("未提供用量")
    return "\n".join(lines)


def usage_picture(records: list[dict], total: int | None = None) -> PictureDocument:
    """Use one column per available metric, preserving missing cells as unknown."""
    if not records:
        if total is None:
            raise ValueError("上下文用量图片缺少数据。")
        return PictureDocument(
            "上下文用量",
            "最近一次 · tokens · 无明细",
            (),
            PictureTable(("轮次", "时间", "合计"), (("—", "—", f"{total:,}"),)),
        )
    keys = [
        key
        for key in USAGE_LABELS
        if any(key in record["values"] for record in records)
    ]
    columns = ("轮次", "时间", *(USAGE_LABELS[key] for key in keys))
    if not keys:
        columns += ("用量",)
    rows = []
    missing = False
    for record in records:
        cells = []
        for key in keys:
            if key in record["values"]:
                cells.append(f"{record['values'][key]:,}")
            else:
                cells.append("—")
                missing = True
        if not keys:
            cells.append("未提供用量")
        rows.append(
            (
                str(record["round"]),
                datetime.fromisoformat(record["time"]).strftime("%m-%d %H:%M:%S"),
                *cells,
            )
        )
    summary = f"最近 {len(records)} 轮 · tokens"
    if missing:
        summary += " · — 未提供"
    return PictureDocument(
        "上下文用量", summary, (), PictureTable(columns, tuple(rows))
    )
