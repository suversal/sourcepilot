"""从原始响应里按配置取值。三种格式共用一套字段定义。

- JSON：点分路径 + 数组下标。刻意不上 jsonpath，够热榜用就行。
- HTML：CSS 选择器（相对当前行），取文本或属性。
- RSS ：先把条目压成普通 dict，再走 JSON 那条路——这样只有一套取值逻辑。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urljoin

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


def select_html(row: Any, spec: FieldSpec) -> str | None:
    """按 CSS 选择器从行元素里取文本或属性。`select: "."` 表示行元素自身。"""
    target = row if spec.select in (".", "") else row.select_one(spec.select or "")
    if target is None:
        return None
    if spec.attr:
        value = target.get(spec.attr)
        if isinstance(value, list):  # class 之类的多值属性
            value = " ".join(value)
        return value
    return re.sub(r"\s+", " ", target.get_text(" ", strip=True)).strip() or None


def render_template(
    template: str,
    *,
    row: Any = None,
    extracted: dict[str, Any] | None = None,
    base_url: str = "",
) -> str | None:
    """`{base_url}{href}` → 填空。

    取值顺序：已抽出的字段 → base_url → 原始行的 JSON 路径。
    任一占位取不到就整体返回 None——半截 URL 比没有 URL 更糟。
    """
    extracted = extracted or {}
    missing = False

    def sub(match: re.Match[str]) -> str:
        nonlocal missing
        expr = match.group(1)
        key, _, filt = expr.partition("|")
        key = key.strip()

        if key in extracted and extracted[key] is not None:
            value = extracted[key]
        elif key == "base_url":
            value = base_url
        elif isinstance(row, dict | list):
            value = resolve_path(row, key)
        else:
            value = None

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


def extract_row(
    row: Any,
    fields: dict[str, FieldSpec],
    *,
    is_html: bool,
    base_url: str = "",
) -> dict[str, Any]:
    """抽出一整行。先做 path/select，再做 template——模板才能引用前面的结果。"""
    out: dict[str, Any] = {}

    for key, spec in fields.items():
        if spec.template is not None:
            continue
        raw = select_html(row, spec) if is_html else resolve_path(row, spec.path or "")
        value = coerce(raw, spec.type)
        out[key] = spec.default if value is None else value

    for key, spec in fields.items():
        if spec.template is None:
            continue
        raw = render_template(
            spec.template, row=row, extracted=out, base_url=base_url
        )
        value = coerce(raw, spec.type)
        out[key] = spec.default if value is None else value

    # 相对链接补全：模板里已经拼过 base_url 的不受影响（urljoin 对绝对地址是幂等的）。
    if base_url and isinstance(out.get("url"), str):
        out["url"] = urljoin(base_url, out["url"])
    return out
