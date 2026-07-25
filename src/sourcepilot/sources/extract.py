"""从原始 JSON 里按配置取值。

刻意做得很小：点分路径 + 数组下标 + 模板拼接，够热榜用。
需要更强的表达力时再换 jsonpath，不提前上依赖。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .config import FieldSpec

_TEMPLATE_SLOT = re.compile(r"\{([^{}]+)\}")


def resolve_path(data: Any, path: str) -> Any:
    """`data.list.0.title` → 逐段下钻。任一段取不到返回 None，不抛。"""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            if not part.lstrip("-").isdigit():
                return None
            idx = int(part)
            cur = cur[idx] if -len(cur) <= idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def render_template(template: str, row: Any) -> str | None:
    """`https://…/{bvid}` → 用行内字段填空。支持 `{path|urlencode}`。"""
    missing = False

    def sub(match: re.Match[str]) -> str:
        nonlocal missing
        expr = match.group(1)
        path, _, filt = expr.partition("|")
        value = resolve_path(row, path.strip())
        if value is None:
            missing = True
            return ""
        text = str(value)
        return quote(text, safe="") if filt.strip() == "urlencode" else text

    rendered = _TEMPLATE_SLOT.sub(sub, template)
    return None if missing else rendered


def coerce(value: Any, kind: str) -> Any:
    if value is None:
        return None
    try:
        match kind:
            case "str":
                text = str(value).strip()
                return text or None
            case "int":
                return int(value)
            case "float":
                return float(value)
            case "unix":
                ts = float(value)
                # 毫秒时间戳自动降级到秒
                if ts > 1e11:
                    ts /= 1000
                return datetime.fromtimestamp(ts, tz=UTC)
            case "iso":
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return None


def extract_field(row: Any, spec: FieldSpec) -> Any:
    raw = (
        render_template(spec.template, row)
        if spec.template is not None
        else resolve_path(row, spec.path or "")
    )
    value = coerce(raw, spec.type)
    return spec.default if value is None else value
