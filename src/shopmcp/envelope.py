"""Tool response envelope: structuredContent plus the same JSON, compact, as text."""

import json
from typing import Any

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel

from shopmcp.errors import ShopError
from shopmcp.i18n import Texts


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _result(data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=_compact(data))], structured_content=data
    )


def _dump(payload: BaseModel) -> dict[str, Any]:
    return payload.model_dump(mode="json", exclude_none=True)


def size(payload: BaseModel) -> int:
    """Bytes of the text content `ok(payload)` would produce."""
    return len(_compact(_dump(payload)).encode())


def ok(payload: BaseModel, max_bytes: int | None = None) -> CallToolResult:
    """Success result. Over `max_bytes`, a paged payload (`meta` + `items`) loses items
    from the end and gets `meta.truncated = meta.has_more = True`."""
    data = _dump(payload)
    if max_bytes is None or len(_compact(data).encode()) <= max_bytes:
        return _result(data)
    items = data.get("items")
    meta = data.get("meta")
    if not isinstance(items, list) or not isinstance(meta, dict) or "truncated" not in meta:
        return _result(data)
    meta["truncated"] = meta["has_more"] = True
    lo, hi = 0, len(items)  # largest prefix that fits, by bisection
    while lo < hi:
        mid = (lo + hi + 1) // 2
        data["items"] = items[:mid]
        if len(_compact(data).encode()) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    data["items"] = items[:lo]
    return _result(data)


def fail(err: ShopError, texts: Texts) -> CallToolResult:
    """Error result: isError=true, no structuredContent (it would not match outputSchema)."""
    error: dict[str, Any] = {
        "code": err.code,
        "message": err.message or texts.get(f"error.{err.code}", type(err).default_message),
        "retryable": err.retryable,
    }
    if err.details:
        error["details"] = err.details
    return CallToolResult(
        content=[TextContent(type="text", text=_compact({"error": error}))], is_error=True
    )
