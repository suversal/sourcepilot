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

from .channels.x.tweet import classify as classify_tweet
from .channels.x.tweet import display_fields as tweet_display_fields
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
    raw           TEXT NOT NULL,
    -- 这条是怎么进库的：`collected` = 平台自己订阅采集的，`searched` = 某次
    -- 现查（search_x / get_x_timeline）顺手落下的缓存。
    -- 两者必须分开：现查的 query 是调用方随口给的，搜「Opus」会捞回
    -- 「magnum opus」这种毫不相干的推文，混进信息流就会推给所有 RSS 订阅者
    -- 和 AIRADAR。落库仍然要做——降级链靠它兜底——但只在降级时才读。
    origin        TEXT NOT NULL DEFAULT 'collected'
);
CREATE INDEX IF NOT EXISTS idx_items_discovered ON items(discovered_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_platform   ON items(platform, score DESC);
CREATE INDEX IF NOT EXISTS idx_items_type       ON items(source_type, discovered_at DESC);

-- 推文全貌。与 items 是**同一条推文的两个视图**，不是主从关系：
-- items 进信息流参与跨源检索，这张表供需要推文原貌的消费方（推文卡片）使用。
-- 之所以不合并进 items：互动数、引用链、线程、展开外链在别的信源里没有对应
-- 概念，塞进 items.raw 的话消费方就不能依赖它了（契约声明 raw 结构不稳定）。
CREATE TABLE IF NOT EXISTS x_tweets (
    tweet_id           TEXT PRIMARY KEY,
    conversation_id    TEXT,
    author_handle      TEXT NOT NULL,
    author_name        TEXT,
    author_id          TEXT,
    author_avatar      TEXT,
    author_verified    INTEGER NOT NULL DEFAULT 0,
    author_followers   INTEGER,
    text               TEXT NOT NULL,
    lang               TEXT,
    created_at         TEXT,
    likes              INTEGER,
    retweets           INTEGER,
    replies            INTEGER,
    quotes             INTEGER,
    bookmarks          INTEGER,
    views              INTEGER,
    is_reply           INTEGER NOT NULL DEFAULT 0,
    reply_to_handle    TEXT,
    reply_to_tweet_id  TEXT,
    is_quote           INTEGER NOT NULL DEFAULT 0,
    quoted_tweet_id    TEXT,
    quoted_handle      TEXT,
    quoted_text        TEXT,
    -- 以下四个是 JSON 数组。urls 里的 expanded_url 是**展开后的真实地址**，
    -- 正文里那些 t.co 短链解析不动也不该去解析（慢，且会在对方统计里留点击）。
    urls               TEXT NOT NULL DEFAULT '[]',
    hashtags           TEXT NOT NULL DEFAULT '[]',
    mentions           TEXT NOT NULL DEFAULT '[]',
    media              TEXT NOT NULL DEFAULT '[]',
    possibly_sensitive INTEGER NOT NULL DEFAULT 0,
    source_client      TEXT,
    -- X 长文（Articles）。推文本身只是个入口，正文在这里。
    -- has_article=1 而 article_markdown 为空 = 还没去取正文（要单独一次请求）。
    has_article        INTEGER NOT NULL DEFAULT 0,
    article_id         TEXT,
    article_title      TEXT,
    article_markdown   TEXT,
    article_summary    TEXT,
    article_cover      TEXT,
    fetched_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON x_tweets(created_at DESC, tweet_id DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_author  ON x_tweets(author_handle, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_convo   ON x_tweets(conversation_id);

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
    last_item_count      INTEGER NOT NULL DEFAULT 0,
    -- HTTP 条件请求的校验器。带上它们再请求，对方内容没变就回 304 空响应，
    -- 一个字节的正文都不用传，解析与入库也整个跳过。
    etag                 TEXT,
    last_modified        TEXT
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
        state_cols = {r["name"] for r in conn.execute("PRAGMA table_info(source_state)")}
        for col in ("etag", "last_modified"):
            if col not in state_cols:
                conn.execute(f"ALTER TABLE source_state ADD COLUMN {col} TEXT")
        # x_tweets 的长文列。这张表可能在长文功能之前就建好了，
        # CREATE TABLE IF NOT EXISTS 不会给已存在的表补列。
        tweet_cols = {r["name"] for r in conn.execute("PRAGMA table_info(x_tweets)")}
        if tweet_cols:  # 表存在才补，否则 SCHEMA 刚建的已经是全的
            for col, decl in (
                ("has_article", "INTEGER NOT NULL DEFAULT 0"),
                ("article_id", "TEXT"),
                ("article_title", "TEXT"),
                ("article_markdown", "TEXT"),
                ("article_summary", "TEXT"),
                ("article_cover", "TEXT"),
            ):
                if col not in tweet_cols:
                    conn.execute(f"ALTER TABLE x_tweets ADD COLUMN {col} {decl}")

        if "origin" not in existing:
            # 老库一律回填 collected。这会把历史上那几条现查缓存也当成采集内容，
            # 但反过来（默认 searched）会让整个库从信息流里消失，那个错法严重得多。
            conn.execute(
                "ALTER TABLE items ADD COLUMN origin TEXT NOT NULL DEFAULT 'collected'"
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

    def upsert_items(self, items: Iterable[Item], *, origin: str = "collected") -> int:
        """落库。`origin` 区分订阅采集与现查缓存，见 items.origin 的列注释。"""
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
                origin,
            )
            for item in items
        ]
        if not rows:
            return 0
        with self._conn() as conn:
            # discovered_at 保持首次收录时间不变——增量拉取（since）依赖它稳定。
            conn.executemany(
                """
                INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, summary=excluded.summary, url=excluded.url,
                    author=excluded.author, published_at=excluded.published_at,
                    -- 用库里已存的 discovered_at 兜底，不能用 excluded 的。
                    -- 重抓时新对象的 discovered_at 是「现在」，跟着它走会让没有
                    -- 发布时间的条目每采集一轮就往前漂一次——永远浮在信息流顶部、
                    -- 永远落在时间窗内，翻页时还会因为排序键中途变化而漏条重条。
                    effective_at=COALESCE(excluded.published_at, items.discovered_at),
                    time_basis=excluded.time_basis, score=excluded.score,
                    categories=excluded.categories, media=excluded.media, raw=excluded.raw,
                    -- origin 只能单向升级。一条推文先被某次搜索捞到、后来定时采集
                    -- 也抓到了，它就确实属于订阅内容；反过来，已经在采集范围内的
                    -- 条目不该因为有人搜到它而被降级出信息流。
                    origin=CASE WHEN excluded.origin = 'collected'
                                THEN 'collected' ELSE items.origin END
                """,
                rows,
            )
        return len(rows)

    #: x_tweets 的列顺序，插入与读取共用一份，避免两处各写各的而错位。
    TWEET_COLUMNS = (
        "tweet_id", "conversation_id", "author_handle", "author_name", "author_id",
        "author_avatar", "author_verified", "author_followers", "text", "lang",
        "created_at", "likes", "retweets", "replies", "quotes", "bookmarks", "views",
        "is_reply", "reply_to_handle", "reply_to_tweet_id", "is_quote",
        "quoted_tweet_id", "quoted_handle", "quoted_text", "urls", "hashtags",
        "mentions", "media", "possibly_sensitive", "source_client",
        "has_article", "article_id", "article_title", "article_markdown",
        "article_summary", "article_cover", "fetched_at",
    )

    def upsert_tweets(self, records: Iterable[Any]) -> int:
        """写入推文全貌。互动数会随时间涨，所以重复采集时**覆盖**而不是跳过。"""
        rows = []
        for r in records:
            rows.append((
                r.tweet_id, r.conversation_id, r.author_handle, r.author_name, r.author_id,
                r.author_avatar, int(r.author_verified), r.author_followers, r.text, r.lang,
                _iso(r.created_at) if r.created_at else None,
                r.likes, r.retweets, r.replies, r.quotes, r.bookmarks, r.views,
                int(r.is_reply), r.reply_to_handle, r.reply_to_tweet_id, int(r.is_quote),
                r.quoted_tweet_id, r.quoted_handle, r.quoted_text,
                json.dumps(r.urls, ensure_ascii=False),
                json.dumps(r.hashtags, ensure_ascii=False),
                json.dumps(r.mentions, ensure_ascii=False),
                json.dumps(r.media, ensure_ascii=False),
                int(r.possibly_sensitive), r.source_client,
                int(r.has_article), r.article_id, r.article_title, r.article_markdown,
                r.article_summary, r.article_cover, _iso(r.fetched_at),
            ))
        if not rows:
            return 0
        # 显式列名而不是 `VALUES (?,?,…)` 位置插入：迁移用 ALTER TABLE 加的列
        # 会追加在**表末尾**，与这里的声明顺序不一致，位置插入就会静默错位
        # ——这个 bug 真实发生过，表现是 NOT NULL 约束在一个明明有默认值的列上失败。
        columns = ",".join(self.TWEET_COLUMNS)
        placeholders = ",".join("?" * len(self.TWEET_COLUMNS))
        # 正文字段用 COALESCE 保护：它是单独一次请求换来的，而常规采集
        # （搜索/时间线）拿不到正文，直接覆盖会把已抓好的全文抹成 NULL。
        protected = {"article_markdown", "article_title", "article_summary", "article_cover"}
        updates = ",".join(
            (f"{c}=COALESCE(excluded.{c}, {c})" if c in protected else f"{c}=excluded.{c}")
            for c in self.TWEET_COLUMNS
            if c != "tweet_id"
        )
        with self._conn() as conn:
            conn.executemany(
                f"INSERT INTO x_tweets ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(tweet_id) DO UPDATE SET {updates}",
                rows,
            )
        return len(rows)

    def query_tweets(
        self,
        *,
        q: str | None = None,
        handle: str | None = None,
        conversation_id: str | None = None,
        has_links: bool = False,
        has_article: bool = False,
        missing_article_text: bool = False,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """读推文全貌。JSON 列在这里解开，消费方拿到的就是可直接用的结构。"""
        where, args = [], []
        if q:
            where.append("text LIKE ?")
            args.append(f"%{q}%")
        if handle:
            where.append("author_handle = ? COLLATE NOCASE")
            args.append(handle)
        if conversation_id:
            where.append("conversation_id = ?")
            args.append(conversation_id)
        if has_links:
            # 空数组是 '[]'，长度 2；有内容就更长。
            where.append("length(urls) > 2")
        if has_article:
            where.append("has_article = 1")
        if missing_article_text:
            # 「知道有长文但还没取正文」——补抓任务就是照这个清单干活的。
            where.append("has_article = 1 AND (article_markdown IS NULL OR article_markdown = '')")
        if since is not None:
            where.append("created_at > ?")
            args.append(_iso(since))
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        args.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM x_tweets{clause} ORDER BY created_at DESC, tweet_id DESC LIMIT ?",
                args,
            ).fetchall()
        return [_row_to_tweet(r) for r in rows]

    def query_thread(self, conversation_id: str, author_only: bool = True) -> list[dict[str, Any]]:
        """取一整串线程，**按时间正序**。

        作者连发五条讲一件事，拆成五个卡片会很碎——合起来才是一篇内容。

        `author_only` 默认开着，它滤掉两种东西：

        1. **别人的回复**——同一个 conversation_id 下混着所有人的评论。
        2. **作者回复别人的那些**。这条容易漏：作者在自己线程下回复某个网友的
           提问（「@某某 机制啊」），作者是他本人、conversation_id 也对，但那是
           评论区互动而不是他要讲的内容。判据是 `reply_to_handle` 指向别人。

        作者接着自己发（`reply_to_handle` 是他自己）才是线程的续写。
        原作者取最早那条的作者——线程的第一条按定义就是发起者。
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM x_tweets WHERE conversation_id = ? "
                "ORDER BY created_at ASC, tweet_id ASC",
                (conversation_id,),
            ).fetchall()
        tweets = [_row_to_tweet(r) for r in rows]
        if author_only and tweets:
            starter = tweets[0]["author_handle"]
            tweets = [
                t
                for t in tweets
                if t["author_handle"] == starter
                and t["reply_to_handle"] in (None, "", starter)
            ]
        return tweets

    def count_tweets(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM x_tweets").fetchone()[0]

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
        include_searched: bool = False,
    ) -> list[Item]:
        where: list[str] = []
        args: list[Any] = []

        if not include_searched:
            # 默认只给订阅采集的内容。现查缓存是调用方随口给的 query 捞回来的，
            # 混进信息流就会推给所有下游——只有 X 的降级链该看见它们。
            where.append("origin = 'collected'")

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

    def touch_success(self, name: str, at: datetime) -> None:
        """记一次「成功但内容没变」（HTTP 304）。

        刻意不动 `last_item_count`——它的含义是「上次真抓到多少条」，
        304 那轮写 0 会让 /health 和 Canary 以为这个源突然没数据了。
        """
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE source_state
                SET last_attempt_at=?, last_success_at=?,
                    last_error_code=NULL, consecutive_failures=0
                WHERE name=?
                """,
                (_iso(at), _iso(at), name),
            )

    def save_validators(self, name: str, etag: str | None, last_modified: str | None) -> None:
        """记下本次响应的 ETag / Last-Modified，下次带着去问「变了吗」。"""
        with self._conn() as conn:
            conn.execute(
                "UPDATE source_state SET etag=?, last_modified=? WHERE name=?",
                (etag, last_modified, name),
            )

    def get_validators(self, name: str) -> tuple[str | None, str | None]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT etag, last_modified FROM source_state WHERE name=?", (name,)
            ).fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

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


def _row_to_tweet(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("urls", "hashtags", "mentions", "media"):
        d[key] = json.loads(d[key] or "[]")
    for key in ("author_verified", "is_reply", "is_quote", "possibly_sensitive", "has_article"):
        d[key] = bool(d[key])
    # 展开后的站外链接，下游抓原文直接用这个，不必再解析 t.co。
    d["external_urls"] = [
        u["expanded_url"]
        for u in d["urls"]
        if u.get("expanded_url")
        and "//x.com/" not in u["expanded_url"]
        and "//twitter.com/" not in u["expanded_url"]
    ]
    d["url"] = f"https://x.com/{d['author_handle']}/status/{d['tweet_id']}"
    # 派生字段：读取时算而不是存库。规则以后要调，存库的话历史数据会跟新规则不一致。
    d["content_kind"] = classify_tweet(d)
    d["display_title"], d["display_text"] = tweet_display_fields(d)
    return d


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
