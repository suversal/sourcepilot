"""SQLite 条目库。

存两样东西：归一化后的条目，以及每个源的采集状态（自适应间隔与 Canary 都要用）。
"""

from __future__ import annotations

import json
import sqlite3
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    BadRequest,
    Category,
    ErrorCode,
    Item,
    Media,
    Source,
    SourceType,
    TimeBasis,
)
from .settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    source_type   TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    platform      TEXT,
    title         TEXT NOT NULL,
    summary       TEXT,
    url           TEXT NOT NULL,
    author        TEXT,
    published_at  TEXT,
    discovered_at TEXT NOT NULL,
    -- 发布时间，取不到就退回收录时间。信息流的排序与时间窗都按它算：
    -- 按 discovered_at 排会让「首次采集」把陈年旧文全变成今天的新闻。
    effective_at  TEXT NOT NULL,
    time_basis    TEXT NOT NULL,
    score         REAL NOT NULL,
    categories    TEXT NOT NULL,
    lang          TEXT,
    media         TEXT NOT NULL,
    raw           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_platform   ON items(platform, score DESC);
CREATE INDEX IF NOT EXISTS idx_items_type       ON items(source_type, discovered_at DESC);

CREATE TABLE IF NOT EXISTS cooldowns (
    key         TEXT PRIMARY KEY,
    until       TEXT NOT NULL,
    error_code  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_state (
    name                 TEXT PRIMARY KEY,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_error_code      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_item_count      INTEGER NOT NULL DEFAULT 0
);
"""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value.replace("Z", "+00:00"))


class Store:
    def __init__(self, path: Path | str = DB_PATH) -> None:
        self.path = Path(path)
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """补齐老库缺的列。`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列。"""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        if "effective_at" not in existing:
            conn.execute("ALTER TABLE items ADD COLUMN effective_at TEXT")
            conn.execute(
                "UPDATE items SET effective_at = COALESCE(published_at, discovered_at)"
            )
        # 索引必须建在这里而不是 SCHEMA 里——老库补列之前，SCHEMA 里的建索引语句
        # 会因为列不存在而整段失败。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_effective "
            "ON items(effective_at DESC, id DESC)"
        )
        # 修复历史漂移：曾经的 upsert 会把没有发布时间的条目的 effective_at
        # 跟着每轮重抓往前推。重算一遍，让它回到首次收录时间。
        conn.execute(
            "UPDATE items SET effective_at = COALESCE(published_at, discovered_at) "
            "WHERE effective_at != COALESCE(published_at, discovered_at)"
        )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---------- 条目 ----------

    def upsert_items(self, items: Iterable[Item]) -> int:
        rows = [
            (
                item.id,
                item.source.type.value,
                item.source.name,
                item.source.platform,
                item.title,
                item.summary,
                str(item.url),
                item.author,
                _iso(item.published_at) if item.published_at else None,
                _iso(item.discovered_at),
                _iso(item.effective_time),
                item.time_basis.value,
                item.score,
                json.dumps([c.value for c in item.categories]),
                item.lang,
                json.dumps([m.model_dump(mode="json") for m in item.media]),
                json.dumps(item.raw, ensure_ascii=False, default=str),
            )
            for item in items
        ]
        if not rows:
            return 0
        with self._conn() as conn:
            # discovered_at 保持首次收录时间不变——增量拉取（since）依赖它稳定。
            conn.executemany(
                """
                INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, summary=excluded.summary, url=excluded.url,
                    author=excluded.author, published_at=excluded.published_at,
                    -- 用库里已存的 discovered_at 兜底，不能用 excluded 的。
                    -- 重抓时新对象的 discovered_at 是「现在」，跟着它走会让没有
                    -- 发布时间的条目每采集一轮就往前漂一次——永远浮在信息流顶部、
                    -- 永远落在时间窗内，翻页时还会因为排序键中途变化而漏条重条。
                    effective_at=COALESCE(excluded.published_at, items.discovered_at),
                    time_basis=excluded.time_basis, score=excluded.score,
                    categories=excluded.categories, media=excluded.media, raw=excluded.raw
                """,
                rows,
            )
        return len(rows)

    def query_items(
        self,
        *,
        platforms: Sequence[str] | None = None,
        source_type: SourceType | None = None,
        category: Category | None = None,
        q: str | None = None,
        since: datetime | None = None,
        published_after: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
        order_by_score: bool = False,
    ) -> list[Item]:
        where: list[str] = []
        args: list[Any] = []

        if platforms:
            where.append(f"platform IN ({','.join('?' * len(platforms))})")
            args.extend(platforms)
        if source_type is not None:
            where.append("source_type = ?")
            args.append(source_type.value)
        if category is not None:
            where.append("categories LIKE ?")
            args.append(f'%"{category.value}"%')
        if q:
            # 子串匹配而不是 FTS5：FTS5 的两种分词器对中文都不好使——unicode61 把
            # 整串中文当一个词（搜「旗舰」落不到「新一代旗舰模型」），trigram 又要求
            # 查询至少 3 个字符（搜「智谱」直接落空），而中文两字查询极常见。
            # 现阶段全表扫描是亚毫秒级；等条目上十万再换方案（可考虑 jieba 分词 + FTS5）。
            where.append("(title LIKE ? OR summary LIKE ?)")
            like = f"%{q}%"
            args.extend([like, like])
        if since is not None:
            # 增量同步：问的是「上次拉取之后我们又收到了什么」，所以看收录时间。
            where.append("discovered_at > ?")
            args.append(_iso(since))
        if published_after is not None:
            # 时间窗：问的是「最近发生了什么」，所以看发布时间。
            where.append("effective_at >= ?")
            args.append(_iso(published_after))

        if cursor is not None:
            cur_time, cur_id = decode_cursor(cursor)
            where.append("(effective_at < ? OR (effective_at = ? AND id < ?))")
            args.extend([cur_time, cur_time, cur_id])

        order = "score DESC, id DESC" if order_by_score else "effective_at DESC, id DESC"
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        sql = f"SELECT * FROM items{clause} ORDER BY {order} LIMIT ?"
        args.append(limit)

        with self._conn() as conn:
            return [_row_to_item(r) for r in conn.execute(sql, args).fetchall()]

    def count_items_before(self, source_type: SourceType, cutoff: datetime) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM items WHERE source_type = ? AND effective_at < ?",
                (source_type.value, _iso(cutoff)),
            ).fetchone()[0]

    def delete_items_before(self, source_type: SourceType, cutoff: datetime) -> int:
        """按发布时间删。用 discovered_at 的话，一篇今天才被发现的旧文会被立刻删掉。"""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM items WHERE source_type = ? AND effective_at < ?",
                (source_type.value, _iso(cutoff)),
            )
            return cur.rowcount

    def count_items(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # ---------- 源状态 ----------

    def get_state(self, name: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM source_state WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        state = dict(row)
        state["last_success_at"] = _parse(state["last_success_at"])
        state["last_attempt_at"] = _parse(state["last_attempt_at"])
        return state

    def all_states(self) -> dict[str, dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT name FROM source_state").fetchall()
        return {r["name"]: self.get_state(r["name"]) for r in rows}  # type: ignore[misc]

    def record_success(self, name: str, item_count: int, at: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO source_state
                    (name, last_attempt_at, last_success_at, last_error_code,
                     consecutive_failures, last_item_count)
                VALUES (?, ?, ?, NULL, 0, ?)
                ON CONFLICT(name) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=excluded.last_success_at,
                    last_error_code=NULL,
                    consecutive_failures=0,
                    last_item_count=excluded.last_item_count
                """,
                (name, _iso(at), _iso(at), item_count),
            )

    def record_failure(self, name: str, code: ErrorCode, at: datetime) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO source_state
                    (name, last_attempt_at, last_success_at, last_error_code,
                     consecutive_failures, last_item_count)
                VALUES (?, ?, NULL, ?, 1, 0)
                ON CONFLICT(name) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,
                    last_error_code=excluded.last_error_code,
                    consecutive_failures=source_state.consecutive_failures + 1
                """,
                (name, _iso(at), code.value),
            )


    # ---------- 冷却 ----------
    # 冷却必须落盘：进程内的话，重启一次就会立刻去捅一个刚被封的账号。
    # 这是账号安全问题，不是体验问题。

    def save_cooldown(self, key: str, until: datetime, code: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cooldowns VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET until=excluded.until, "
                "error_code=excluded.error_code",
                (key, _iso(until), code),
            )

    def load_cooldowns(self, now: datetime) -> dict[str, tuple[datetime, str]]:
        """读出仍在生效的冷却，顺手清掉过期的。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM cooldowns WHERE until <= ?", (_iso(now),))
            rows = conn.execute("SELECT key, until, error_code FROM cooldowns").fetchall()
        return {r["key"]: (_parse(r["until"]), r["error_code"]) for r in rows}  # type: ignore[misc]

    def clear_cooldown(self, key: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM cooldowns WHERE key = ?", (key,))


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        id=row["id"],
        source=Source(
            type=SourceType(row["source_type"]),
            name=row["source_name"],
            platform=row["platform"],
        ),
        title=row["title"],
        summary=row["summary"],
        url=row["url"],
        author=row["author"],
        published_at=_parse(row["published_at"]),
        discovered_at=_parse(row["discovered_at"]),  # type: ignore[arg-type]
        time_basis=TimeBasis(row["time_basis"]),
        score=row["score"],
        categories=[Category(c) for c in json.loads(row["categories"])],
        lang=row["lang"],
        media=[Media(**m) for m in json.loads(row["media"])],
        raw=json.loads(row["raw"]),
    )


# ---------- 游标 ----------
# opaque：编码方式随时可能变，消费方不得解析（契约 §4）。


def encode_cursor(item: Item) -> str:
    payload = f"{_iso(item.effective_time)}|{item.id}"
    return urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = urlsafe_b64decode(padded.encode()).decode()
        stamp, _, item_id = decoded.partition("|")
        if not stamp or not item_id:
            raise ValueError
        return stamp, item_id
    except Exception as exc:
        raise BadRequest("cursor 无效——它是不透明的，请原样回传上次响应里的值") from exc
