# SourcePilot · 工具契约 v1.9.0

> 采集平台与一切消费方（AIRADAR / MCP 客户端 / Agent）之间的**唯一合同**。
> REST、MCP、SKILL.md 三个出口共用本契约，只是协议壳不同。
>
> 契约版本独立于实现版本。破坏性变更 → 升 major 并新开 `/api/v2`；
> 新增可选字段 → 升 minor，消费方无需改动。
> 每个响应的 `meta.contract_version` 回报当前契约版本。

代码定义在 [`src/sourcepilot/contracts/`](../src/sourcepilot/contracts/)，本文档与代码同步；
**冲突时以代码为准**（pydantic 模型是可执行的契约）。

---

## 0. 本文档相对开发文档的 6 处修订

开发文档 §5 定的契约有 6 处内部矛盾或空缺，会在实现时炸开。逐条决议如下，
后续以本文档为准：

| # | 原文问题 | 本契约决议 |
|---|---|---|
| 1 | `search_x.window` 枚举写成 `实时现查\|24h\|7d`，把取数模式塞进时间窗，又与 `live` 重叠 | 彻底拆开：`window` 只表时间范围，`live` 只控是否允许现查，`meta.mode` 回报实际走了哪条路 |
| 2 | `get_feed.category` 需要分类能力，但铁律禁止采集侧做 LLM 分析 | 改为 `categories`（多标签、非互斥），由**确定性规则**打标（源级映射 + 关键词表，配置在 `config/categories.yaml`），无匹配则空数组。不引入 LLM |
| 3 | `read_article` 返回正文 Markdown，套不进 Item | 独立 `Article` 出参模型，不复用 Item |
| 4 | 三个工具三套分页（`cursor` / `since`+`cursor`+`has_more` / 无） | 统一 opaque `cursor` 入参 + `meta.next_cursor` + `meta.has_more`。`since` 保留但降级为**过滤条件**，与分页游标正交 |
| 5 | `score` 没有值域约定 | 固定 `[0.0, 1.0]`，含义是**源内相对热度**，明确声明**不保证跨源可比**；原始热度值保留在 `raw` |
| 6 | `stale`/`mode` 只在 `search_x` 出参里，但降级是全局机制 | 提升到信封的 `meta` 层，所有工具一致 |

附带两项补充：

- **错误码补 `TIMEOUT` 和 `INTERNAL`**。注意 `TIMEOUT` 只在「现查超时且无缓存可降级」时使用；
  若成功降级到缓存，那是 `ok: true` + `meta.stale: true`，**不是错误**。
- **`published_at` 为空不再回填**。原文说「回退 `discovered_at` 并标注」，但回填后下游无法
  再区分。改为 `published_at` 保持 `null`，另设 `time_basis` 字段显式声明时间依据。

---

## 1. 统一响应信封

REST 与 MCP 完全一致：

```jsonc
{
  "ok": true,
  "data": { /* 各工具自己的出参，见 §4 */ },
  "meta": {
    "contract_version": "1.8.0",
    "mode": "live",              // live | cache | mixed | null(不涉及取数)
    "stale": false,              // true = 降级得到的近似结果，非实时
    "collected_at": "2026-07-25T10:03:00Z",  // 数据快照时间；现查时≈请求时间
    "next_cursor": null,         // 下一页游标，null = 没有下一页
    "has_more": false,
    "elapsed_ms": 412,
    "sources": [                 // 多源工具的分源结果，用于可观测与部分降级
      { "name": "weibo", "ok": true,  "from_cache": true,  "item_count": 20, "error_code": null },
      { "name": "zhihu", "ok": false, "from_cache": false, "item_count": 0,  "error_code": "UPSTREAM_DOWN" }
    ]
  },
  "error": null
}
```

失败时 `ok: false`、`data: null`、`error: { code, message }`。

**关键约定**：

- `ok` 描述的是**请求是否得到可用结果**，不是「是否一切完美」。
  部分源失败但仍有数据 → `ok: true`，失败详情在 `meta.sources`。
- **降级不是错误**。现查失败但缓存兜住了 → `ok: true` + `stale: true` + `mode: "cache"`。
  Agent 据此在简报里标注「非实时」，而不是报错。
- `meta` 永远存在，即使失败（此时 `contract_version` 仍有效，其余字段为默认值）。

### HTTP 状态码映射（仅 REST）

| 错误码 | HTTP | 语义 |
|---|---|---|
| `BAD_REQUEST` | 400 | 参数错误 |
| `NOT_FOUND` | 404 | 内容不存在 |
| `RATE_LIMITED` | 429 | 临时限流，可退避重试 |
| `TIMEOUT` | 504 | 现查超时且无缓存可降级 |
| `UPSTREAM_DOWN` | 502 | 信源不可达 |
| `CAPTCHA` | 502 | 触发验证码，不硬刚 |
| `AUTH_EXPIRED` | 503 | 内部账号失效（**对外不暴露账号细节**） |
| `INTERNAL` | 500 | 平台自身故障 |

MCP 侧没有 HTTP 状态码，只看信封的 `ok` 与 `error.code`，语义完全一致。
出口实现见 `src/sourcepilot/mcp_server.py`——它由 `TOOL_REGISTRY` 驱动，
tool schema 直接从本契约的 params 模型生成，所以 REST 与 MCP 的参数定义
**不可能对不上**（只有一份）。

---

## 2. 统一条目 schema（Item）

```jsonc
{
  "id": "x:1234567890",
  "source": { "type": "x", "name": "X / Twitter", "platform": null },
  "title": "……",
  "summary": "……",
  "url": "https://x.com/…/status/…",
  "author": "elonmusk",
  "published_at": "2026-07-25T10:00:00Z",
  "discovered_at": "2026-07-25T10:03:00Z",
  "time_basis": "published",
  "score": 0.82,
  "categories": ["model"],
  "lang": "en",
  "media": [ { "type": "image", "url": "…", "width": null, "height": null } ],
  "raw": { }
}
```

### 字段约定

| 字段 | 必填 | 约定 |
|---|---|---|
| `id` | 是 | `{source_type}:{native_id}`，全局唯一。**同源内去重**（重复采集只更新不新增）。跨源**不归并**，见下 |
| `source.type` | 是 | 枚举：`x` `hotlist` `wechat` `rss` `web` `vendor` |
| `source.name` | 是 | 人类可读源名，如 `"X / Twitter"` `"微博热搜"` |
| `source.platform` | 否 | 子平台标识，热榜专用：`weibo` `zhihu` `douyin` `bilibili` … |
| `title` | 是 | 非空。无标题的源（如纯图推文）取正文首 80 字符 |
| `summary` | 否 | **客观摘要，不带观点**。抽取式，不做生成式改写。**X 源恒为推文完整正文**——`title` 是 80 字截断版（契约要求 title 非空而推文没有标题），取正文一律用 `summary` |
| `url` | 是 | 第三方原文链接，必须 http(s) |
| `author` | 否 | 作者标识（X 用 handle，不带 @） |
| `published_at` | 否 | 原文发布时间，ISO8601 UTC。**取不到就是 `null`，绝不回填** |
| `discovered_at` | 是 | 本平台收录时间，ISO8601 UTC |
| `time_basis` | 是 | `published`（`published_at` 可信）或 `discovered`（只有收录时间）。下游展示时间必须据此标注 |
| `score` | 是 | `[0.0, 1.0]`，**源内相对热度**，见下 |
| `categories` | 是 | 字符串数组，可空。确定性规则打标，见下 |
| `lang` | 否 | 主要语言，ISO639-1（`zh` `en` …） |
| `media` | 是 | 数组，可空。`type`: `image` `video` `gif` `audio` |
| `raw` | 是 | 原始响应片段，给下游兜底。**不保证结构稳定，消费方不得依赖** |

### 为什么跨源不去重

同一条新闻被多个源报道时，**每个源各出一条 Item，平台不做归并**。这是有意的：

「一条新闻同时上了 8 个源」本身就是最可靠的热度信号之一，归并会把它抹掉。
下游想判断热点，靠的正是这个重数——而采集侧一旦合并，那个信息就永久丢失了，
下游再也补不回来。

所以边界是：**采集侧保留重数，聚类与热度判断归下游**。这与「排序是 AIRADAR 的
职责」是同一条原则的两面——判断「这两条算不算同一件事」需要语义相似度，
本质上是分析而不是采集，和 `categories` 不做 LLM 分类是一个道理。

消费方要合并展示时，自己按标题相似度聚类即可；平台这边只保证同源内不重复
（同一条重复采集只更新不新增）。

URL 仍会做规范化（剥掉追踪参数），但那是为了让**同一个源**的同一条内容
不因埋点串不同而重复入库，不是为了跨源归并。

### 时间

所有时间戳一律 **ISO8601 UTC 带 `Z`**（如 `2026-07-25T10:00:00Z`）。
输入解析接受带任意时区偏移的 ISO8601，内部立刻归一到 UTC。naive datetime 一律拒绝。

下游需要一个可排序的时间时，用 `published_at ?? discovered_at`，
但展示层必须按 `time_basis` 区分说法——`discovered` 的条目只能写「收录于」，
**不得伪称原文发布时间**。

### score 的确切含义

`score` 是**源内相对热度信号**，归一化到 `[0.0, 1.0]`：

- **本身是排行榜的源**（B站排行榜、头条热榜、HN 首页…）：由榜内排名换算
  （rank 1 → 接近 1.0，末位 → 接近 0.0）。
- X：由互动量（转赞评）在本次结果集内归一化。
- **按时间倒序的源**（RSS、快讯、厂商官网博客、公众号）：固定 `0.0`。
  它们没有名次可言，把「第几个被列出来」换算成分数等于伪造热度信号。
  源配置里用 `ranked` 标记该源属于哪一类，默认 `false`。

**明确不保证跨源可比**。微博热搜第 1 的 `0.98` 和某 RSS 的 `0.0` 不构成「前者更重要」。
跨源排序是 AIRADAR 的职责，它需要自己的权重策略；采集侧只提供源内信号。
原始热度值（阅读数、转发数、榜单分）保留在 `raw` 里供下游自行加权。

### categories 的打标规则

采集侧**不做 LLM 分析**（铁律）。`categories` 由两级确定性规则产生：

1. **源级映射**：某些源天然属于某类（如 arXiv → `paper`）。
2. **关键词规则**：标题 + 摘要命中 `config/categories.yaml` 的关键词/正则。

v1 词表：`model` `product` `paper` `industry` `tip`。允许多标签，无命中则空数组。
规则表是配置不是代码，改分类 = 改 YAML。

消费方约定：`categories` 是**过滤辅助，不是权威分类**。空数组表示「规则没命中」，
不代表「不属于任何类」。

**现状（2026-07-25）：关键词规则默认关闭，多数条目的 `categories` 是空数组。**
实测开着的时候误标率高到没法用——子串匹配让「ChatGPT」命中 `model` 关键词，
于是产品公告被打成模型发布；匹配范围含摘要更是让 RSS 里随口提一句就被带跑。
现在只保留「这个源只发这类内容」的源级映射（如 Qwen 博客 → `model`），
综合性信源一律不打标。

**空数组是诚实的，错标签是有害的**——AIRADAR 拿它筛内容，喂错标签比不给更糟。
更根本地说，准确的主题分类需要语义理解，而铁律禁止采集侧做 LLM 分析；
「用户关心什么」和排序一样，本来就是 AIRADAR 的职责。

---

## 3. 取数模式：现查 vs 缓存

三个正交的概念，别混：

| 概念 | 位置 | 含义 |
|---|---|---|
| `live`（入参） | 请求 | 是否**允许**现查。`false` = 强制只读缓存 |
| `window`（入参） | 请求 | 时间范围过滤，与取数模式**无关** |
| `mode`（出参 meta） | 响应 | **实际**走了什么：`live` / `cache` / `mixed` |
| `stale`（出参 meta） | 响应 | 结果是否为降级的近似值 |

`window` 枚举：`1h` `6h` `24h` `7d` `30d` `all`。
`all` 表示不限时间——检索历史内容时需要它，把人锁在 30 天内会让几个月前的相关条目全看不见。

**降级链**（现查类工具）：

```
live=true → 现查（带超时，默认 8s）
              ├─ 成功       → mode=live,  stale=false
              ├─ 超时/限流/验证码 → 回落缓存
              │                     ├─ 缓存有数据 → mode=cache, stale=true, ok=true
              │                     └─ 缓存也空   → ok=false, error=TIMEOUT/RATE_LIMITED/CAPTCHA
              └─ 参数错误   → ok=false, error=BAD_REQUEST（不降级）
live=false → 只读缓存 → mode=cache, stale=false（这是用户要的，不算降级）
```

注意 `live=false` 时 `stale` 是 **false**——用户明确要缓存，拿到缓存就是正确结果，
`stale` 只标记「你要的是实时，我没给到」。

---

## 4. 工具清单与出入参

| 工具 | REST 端点 | 取数 | 分页 |
|---|---|---|---|
| `search_x` | `GET /api/v1/x/search` | 现查 + 缓存兜底 | 有 |
| `get_x_timeline` | `GET /api/v1/x/timeline` | 现查 + 缓存兜底 | 有 |
| `get_hotlist` | `GET /api/v1/hotlist` | 缓存 | 无 |
| `get_wechat_feed` | `GET /api/v1/wechat/feed` | 缓存 | 有 |
| `read_article` | `GET /api/v1/article` | 现查 | 无 |
| `get_feed` | `GET /api/v1/items` | 缓存 | 有 |

分页统一：入参 `cursor`（opaque 字符串，来自上次响应的 `meta.next_cursor`），
出参 `meta.next_cursor` + `meta.has_more`。
**消费方不得解析 cursor 内容**——它是不透明的，编码方式可能随时变。

### `search_x`

```
入参
  q       string  必填  搜索词
  limit   int     选填  默认 20，上限 100
  window  enum    选填  默认 7d（X 搜索超 7 天不保证可得）
  live    bool    选填  默认 true
  cursor  string  选填
出参 data: { items: Item[] }
     分页与 mode/stale 在 meta
```

### `get_x_timeline`

```
入参
  handle  string  必填  用户 handle，不带 @
  limit   int     选填  默认 20，上限 100
  window  enum    选填  默认 7d
  live    bool    选填  默认 true
  cursor  string  选填
出参 data: { items: Item[] }
```

### `get_hotlist`

```
入参
  platform  string  选填  weibo|zhihu|douyin|bilibili…  不填 = 全部
  limit     int     选填  每平台条数，默认 20，上限 50
出参 data: { items: Item[] }
     meta.collected_at = 缓存快照时间
     meta.sources[]    = 分平台成功/失败（一个平台挂了不拖垮整体）
```

### `get_wechat_feed`

```
入参
  account  string  选填  公众号标识，不填 = 全部已订阅
  window   enum    选填  默认 7d
  limit    int     选填  默认 20，上限 100
  cursor   string  选填
出参 data: { items: Item[] }
```

### `read_article`

**这是平台唯一按调用方给的地址出网的工具**，所以它有一道强制的地址校验：
只接受指向公网的 http(s)，端口限于 80/443/8080/8443，且解析出的 IP 不能是
私网、回环、链路本地或保留地址（云厂商的 `169.254.169.254` 元数据接口在此之列）。
跟随重定向后会**重新校验一次**——否则「公网地址 302 到内网」就绕过了首次检查。
被拒时统一回 `BAD_REQUEST`，且**不透露拒绝原因是「这是内网」**，那本身就是
一条内网探测的反馈信号。

**出参不是 Item**（修订 #3）：

```
入参
  url       string  必填  http(s)
  max_chars int     选填  正文截断上限，默认 50000
出参 data: Article
```

```jsonc
{
  "url": "https://…",
  "title": "……",
  "author": null,
  "published_at": null,
  "content_markdown": "# …",
  "char_count": 4210,
  "truncated": false,
  "lang": "zh",
  "fetched_at": "2026-07-25T10:03:00Z"
}
```

### `get_feed`（喂 AIRADAR）

```
入参
  q         string  选填  关键词，在标题与摘要里做子串匹配（v1.2.0 新增）
  platform  string  选填  按具体信源过滤，**逗号分隔可给多个**（v1.4.0）
                          如 `bilibili,toutiao,juejin`；名字不认识会报 BAD_REQUEST 并列出可用值
  window    enum    选填  默认 24h；检索历史用 all
  category  string  选填  单个分类，匹配 Item.categories 中任一项
  source    string  选填  按 source.type 过滤
  since     string  选填  ISO8601，只返回 discovered_at > since 的条目（过滤条件）
  limit     int     选填  默认 50，上限 200
  cursor    string  选填  分页位置
出参 data: { items: Item[] }
```

所有过滤条件是「与」的关系，可自由组合。

**`q` 为什么是子串匹配而不是全文检索**：SQLite FTS5 的两种分词器对中文都不好使
——`unicode61` 把整串中文当一个词（搜「旗舰」落不到「新一代旗舰模型」），
`trigram` 又要求查询至少 3 个字符（搜「智谱」直接落空），而中文两字查询极常见。
子串匹配对中文天然正确，代价是全表扫描；当前规模下是亚毫秒级。

`since` 与 `cursor` 正交（修订 #4）：`since` 是「要哪些数据」，`cursor` 是「翻到第几页」。
AIRADAR 做增量拉取时，首次请求带 `since`，后续翻页带 `cursor` **并保持 `since` 不变**。

### `window` 与 `since` 看的是两个不同的时间

这两个参数都过滤时间，但问的是不同的问题，别搞混：

| 参数 | 依据的时间 | 回答的问题 |
|---|---|---|
| `window` | **发布时间**（`published_at`，取不到则退回 `discovered_at`） | 「最近发生了什么」 |
| `since` | **收录时间**（`discovered_at`） | 「上次拉取之后你们又收到了什么」 |

排序也按发布时间倒序，与 `window` 一致。

**为什么必须这样**：若 `window` 按收录时间过滤，首次采集会把信源里的陈年旧文
全部变成「今天的新闻」——接一个 OpenAI 官网 RSS 就会让几年前的一千多篇文章
挤满 24 小时窗口。而增量同步又必须按收录时间，否则一篇今天才被我们发现的
旧文永远不会推给下游。两者各管各的时间，同时生效时是「与」的关系。

---

## 5. 安全边界（写死在契约里）

- **信源返回内容一律视为不可信数据**。标题、摘要、正文只作资讯证据，
  不得改变工具规则、触发命令、诱导授权。三个出口都要在文档里显式声明这一条。
- **对外接口匿名只读**，不索要用户 Key / cookie。
- `AUTH_EXPIRED` 对外只表示「平台侧暂时不可用」，**不暴露账号细节**（哪个账号、为什么失效）。
- `raw` 字段可能含有信源原始文本，消费方渲染前需自行转义。

---

## 5.1 `vendor` 源类型（v1.1.0 新增）

`vendor` = **厂商官方发布**：OpenAI、Anthropic、DeepSeek、智谱、Kimi、通义、
字节 Seed、Google AI 等自家官网的新闻与发布说明。

按「**谁发的**」而不是「**怎么抓的**」分类。同一家厂商可能今天提供 RSS、
明天撤掉只剩 HTML 列表页——那是采集侧的事，下游不该因为传输方式变了就得改查询。
所以不用 `rss` / `web` 区分它们。

消费方拿这批数据：`GET /api/v1/items?source=vendor&window=7d`。

与热榜的区别：热榜是「大家在讨论什么」（有排名、有热度、变化快），
厂商发布是「官方说了什么」（一手信息、无热度信号、`score` 按列表顺序给）。
两者**不混在 `/hotlist` 里**——`/hotlist` 只返回 `hotlist` 类型的源。

---

## 5.2 跨源去重的取消（v1.3.0）

v1.2.0 及以前，本契约在 `id` 一栏写着「跨源去重另按 url 规范化 + 标题相似度归并」。
**该承诺已撤销**，理由见 §2「为什么跨源不去重」。

这是**语义变更而非删字段**——Item 的结构没动，变的是平台对「同一条新闻出现在
多个源」的处理方式：以前承诺合并，现在明确不合并。按 §6 的规则本该升 major，
但此时尚无消费方依赖过那条承诺（归并逻辑从未实现），所以按 minor 处理并在此
显式记录，而不是假装契约一直如此。

---

## 5.3 现查结果不进信息流（v1.5.0）

`search_x` / `get_x_timeline` 的现查结果**会落库，但不会出现在 `get_feed`
（`/api/v1/items`）与 RSS 里**。它们只作降级缓存，在同一工具下次现查失败时被读回。

这条边界是必需的：现查的 `q` 和 `handle` 是调用方随口给的。搜「Opus」会捞回
「Barbie is his magnum opus?」这种毫不相干的推文——若混进信息流，任何人的一次
临时查询都会污染所有 RSS 订阅者与 AIRADAR 的内容。**信息流的内容边界该由平台
的订阅配置决定，而不是由最近谁搜了什么决定。**

落库仍然要做：降级链靠它兜底，去掉就等于 X 挂掉时 `search_x` 直接失败。

平台内部用 `items.origin` 区分（`collected` / `searched`），**该字段不出现在
Item 出参里**——消费方不需要知道，它看到的就该是干净的订阅内容。

一条推文若既被搜索捞到、又在订阅账号的时间线里出现，按 `collected` 计
（单向升级，不可反向降级）。

---

## 5.4 推文全貌 `x_tweets`（v1.6.0 新增）

`GET /api/v1/x/tweets` 返回推文的**原貌**，与 `/items?source=x` 是同一批推文的
两个视图，不是主从关系：

| | `/items?source=x` | `/x/tweets` |
|---|---|---|
| 形状 | 跨源统一的 Item | 推文特有字段 |
| 用途 | 信息流、跨源检索 | 渲染推文卡片 |
| 互动数 | 只有归一化的 `score` | likes / retweets / replies / quotes / bookmarks / views |
| 外链 | 正文里是 `t.co` 短链 | `external_urls` 已展开 |
| 引用推文 | 无 | `quoted_handle` + `quoted_text` |
| 线程 | 无 | `conversation_id` |

**为什么不合并进 Item**：互动数、引用链、线程在别的信源里没有对应概念。塞进
`Item.raw` 的话消费方不能依赖它（§2 声明 raw 结构不稳定），而下游要做展示
就需要稳定形状。

**外链已展开，不要再去解析 `t.co`**。X 的响应里 `entities.urls[].expanded_url`
就是真实地址，平台直接存下来。下游自己去请求短链既慢，又会在对方的点击统计里
留下痕迹。`external_urls` 已排除指回 x.com / twitter.com 的自身链接。

**Nitter 抓来的推文不进这张表**。它走 RSS，拿不到互动数与引用链——写一条
互动数全为 0 的记录会让下游以为「这条推文没人理」，那比缺一条更糟。

### X 长文（Articles）

X 的长文是独立于推文的内容形式，推文只是入口。**搜索与时间线接口返回的
article 只有 `preview_text`（约 100 字预览），正文必须单独一次请求**，
平台会为带长文的推文自动补取，结果放在：

| 字段 | 说明 |
|---|---|
| `has_article` | 这条推文是不是长文入口 |
| `article_title` | 长文标题（与推文正文不同） |
| `article_markdown` | **正文，已转 Markdown**（保留标题层级、链接、配图） |
| `article_summary` | 正文前 ~90 字的**机械截断**，逐字忠实原文 |
| `article_ai_summary` | X（Grok）生成的要点归纳。**二手信息**，可能为空 |
| `article_cover` | 封面图 |

正文转 Markdown 而不是给纯文本：X 也提供 `plain_text`，但那一版把二级标题
和正文段落拍平成同样的行、链接只剩锚文本，下游拿到的是一堆看不出结构的段落。

`has_article` 为 true 但 `article_markdown` 为空 = 补取还没跑到（长文每篇
一次请求，单轮有上限，剩下的下轮补）。

**两个摘要分开是有原因的**：X 的 `summary_text` 由 Grok 延迟生成，早抓拿不到、
晚抓才有，而且可能与正文语言不一致（实测见过中文长文配英文摘要）。合成一个
字段的话，同一个字段有时是原文截断、有时是机器概括，**下游没法判断手里是哪种**。
`article_ai_summary` 为空是正常的——不是所有长文 X 都会生成摘要。
两者都**不是全文**，全文只在 `article_markdown`。

### 富文本样式（v1.7.0 新增）

推文与长文里 X 原生支持的**加粗/斜体**不再被拍平：

- **长文正文 `article_markdown`**：行内加粗/斜体转成 Markdown 强调标记
  （`**` / `*`），与已有的标题层级、链接、配图一起构成完整样式。
- **note tweet（>280 长推）**：X 把样式放在 `richtext_tags` 里，本平台
  原样存下（字段形状：`[{from_index, to_index, richtext_types:["Bold"|"Italic"]}]`），
  并在 `display_text` 里织成 Markdown。**`text` 字段保持纯文本不变**——
  下标、子串匹配、去重都依赖它稳定；要带样式的正文一律用 `display_text`。
- **普通短推没有富文本**：X 不支持，`richtext_tags` 恒为空数组。正文里
  肉眼可见的「粗体字」是作者用 Unicode 数学字母硬拼的，那是字符不是样式，
  原样保留。

消费方约定：**`display_text` 自 v1.7.0 起可能含 Markdown 标记**（此前仅
article 类如此，现在 longform 也会），渲染端统一按 Markdown 处理即可；
纯文本场景（通知、去重、搜索索引）用 `text`。

**配图拼接（v1.8.0 新增）**：`display_text` 现在自带图片——

- 普通推文/长推：图片以 `![](url)` 追加在正文末尾；note tweet 声明了行内
  位置的图织在原文位置；视频给「可点击的缩略图」`[![](缩略图)](视频)`。
- 正文里指向这些媒体的 `t.co` 残链会被清掉（图已经在正文里，留一个指向
  同一张图的短链只会碍事）；v1.8.0 之前采集的老数据没存短链映射，清不了，
  原样保留。
- 转发拼的是原推的媒体；article 类不拼（配图已内嵌在 `article_markdown`，
  推文自身的 media 只是长文卡片的封面预览）。

**按 `display_text` 渲染时不要再另行渲染 `media` 数组**，否则同一张图出现
两次。`media` 数组保持原样，供需要自己控制版式的消费方使用。

### 两个维度：`tweet_type` 与 `content_kind`

别混，它们回答不同的问题：

| | 回答什么 | 取值 | 谁定的 |
|---|---|---|---|
| `tweet_type` | 这条推文**是什么** | `original` `reply` `quote` `repost` | X 平台的客观关系 |
| `content_kind` | **该怎么展示** | `repost` `article` `longform` `link` `quote` `brief` | 本平台的确定性规则 |

一条推文**同时有这两个属性**。`is_reply` 与 `is_quote` 可以同时成立（回复某人
时引用了另一条），所以 `tweet_type` 给的是主类型（优先级 repost > quote >
reply > original），精确判断仍用那几个布尔字段。

**转发（`repost`）必须单独识别**：外层那条推文没有自己的内容——正文是
`RT @某某: …` 的截断，互动数记的是转发这个动作，真正的原文与热度都在被转发
的那条上。不识别的话，`@AnthropicAI` 转发 `@claudeai` 的内容会被记成 Anthropic
官方原创，**作者归属直接错**。原作者与原文在 `retweeted_handle` / `retweeted_text`，
`display_text` 也会自动换成原文。

### 展示分流：`content_kind` 与 `display_*`

推文不是一种内容，是几种。一篇 3 万阅读的长文和一句 59 字的吐槽塞进同一个
列表位，两边都不对。读取时按**确定性规则**给出形态（不涉及语义理解，那是
下游的事，同 `categories` 的原则），优先级从高到低：

| `content_kind` | 判据 | 下游怎么处理 |
|---|---|---|
| `repost` | 纯转发 | 内容整个是别人的，展示 `retweeted_*` |
| `article` | 挂了长文 | 走文章流程，**正文已在 `article_markdown`，不必再拉** |
| `longform` | 正文 > 280 字 | 走文章流程，推文本身就是内容 |
| `link` | 带站外链接 | 真内容在 `external_urls`，按普通文章抓 |
| `quote` | 引用他人 | 卡片，嵌套展示 `quoted_handle` + `quoted_text` |
| `brief` | 其余 | 卡片，适合聚合成「N 条在讨论 X」 |

配套两个派生字段，省掉下游在展示层写分支：

- `display_title` — 长文取 `article_title`，其余取首行
- `display_text` — 长文取 `article_markdown`，其余取 `text`

**为什么 `display_text` 对长文必须换成正文**：长文推文的 `text` 只有一句
入口语（「我整理成一篇长文」），按它渲染会把一条 3 万阅读的内容显示成一句废话。

`link` 那类**不在这里解析外链正文**——那要额外出网，是 `read_article` 的活。

### 线程 `GET /api/v1/x/thread`

按 `conversation_id` 取一整串，**时间正序**，附带拼好的 `combined_text`。

`author_only`（默认 true）滤掉两种东西，第二种容易漏：

1. 别人的回复——同一线程下混着所有人的评论；
2. **作者回复别人的那些**。作者在自己线程下回复网友提问（「@某某 机制啊」），
   作者对、`conversation_id` 也对，但那是评论区互动而不是他要讲的内容。
   判据是 `reply_to_handle` 指向别人；指向他自己才是线程的续写。

这张表**不分 `collected` / `searched`**（对比 §5.3）：它记的是「这条推文长什么样」，
谁触发的抓取不改变这个事实。内容边界由 `/items` 那边守。

---

## 5.5 话题订阅（v1.9.0 新增）

账号订阅盖住「官方说了什么」，但盖不住「某个事件下大家在说什么」。
`config/sources/x.yaml` 新增 `topics` 节，把**事件/话题**作为与账号平级的
订阅对象，定时采集像抓时间线一样定时跑这些搜索：

```yaml
topics:
  - name: AI热点                                 # 话题标识（支持中文）
    query: '(OpenAI OR Claude) (发布 OR released) min_faves:50'
    limit: 20
    min_likes: 50                                # 采集侧确定性阈值兜底
    sort: latest                                 # latest：优先新鲜度
    focus_terms: [OpenAI, Claude]                 # 可选：核心词须在主要内容区
    context_terms: [发布, released]               # 可选：动作词须与核心词接近
    focus_window_chars: 600                      # 推文只检查前 600 字；文章标题/摘要全查
    max_term_distance: 300
    per_author_limit: 1                          # 单轮同作者最多一条
```

**与 §5.3 的关系——不是推翻，是同一原则的另一面。** §5.3 的边界原则是
「信息流内容由平台的订阅配置决定，而不是由最近谁搜了什么决定」。写在配置里
的话题**就是订阅配置**，所以其结果进信息流；`search_x` 的临时 query 仍标
`searched`、仍不进信息流。`items.origin` 增加第三个取值 `topic`，
升级链 `collected > topic > searched`（单向，不可降级）。

**噪音控制是确定性闸门**（铁律：采集侧不做 LLM 语义判断）：`query` 里的
`min_faves:`/`lang:` 在上游生效；`min_likes` 是采集侧兜底，挡上游语法失效
（X 改版）后涌进来的裸结果；`focus_terms` 要求核心词出现在 X Article 的
标题/摘要或推文前 `focus_window_chars` 字，防正文末尾堆词；可选的
`context_terms` + `max_term_distance` 要求两组词在同一内容段且相隔不超过指定
字符数；`per_author_limit` 保持 X 原排序并限制单轮同作者占位。未配置这些新增
字段的话行为与原来一致。真正的语义相关性判断仍归下游。

规则收紧后，当前搜索页里未通过内容/点赞校验且曾被旧规则打过标签的条目会撤销
该话题标签；原始推文不删除。若它只因话题搜索进入库且已无其它话题，Item 会从
`topic` 降为 `searched`，因此退出订阅信息流；账号时间线采集的 `collected`
条目不受影响。同作者限额只限制本轮新增，不追溯删除历史有效标签。

**对消费方的可见变化**（都是 minor）：

- `/items` 的结果可能包含话题采集的条目（origin 对外仍不暴露）。
- `x_tweets` 出参新增 `topics` 字段（字符串数组，命中的话题标识，可空）。
- `GET /api/v1/x/tweets` 新增 `topic` 过滤参数（单个话题标识）。
- 一条推文可同时命中多个话题、也可同时在订阅账号时间线里——`topics`
  是**合并**语义，重复采集不清标签。
- `topics[].name` 支持中文，但它是持久化标识而非纯展示文案；改名时必须同步
  迁移 `x_tweets.topics` 的历史标签，否则新旧名称会分裂成两个话题。

话题搜索走登录账号池（与 `search_x` 同一条通道），配额与封号风险相同，
所以话题应**少而精**；每话题每轮一次搜索，跟随 x 源的 `min_interval`。

## 6. 契约变更规则

- 加**可选**字段、加枚举值、加新工具 → minor，消费方不用动。
- 改字段语义、删字段、改必填性、改枚举含义 → major，新开 `/api/v2`，v1 至少保留一个过渡期。
- `raw` 内部结构变化**不算契约变更**（已声明不稳定）。
- 任何变更同步改 `src/sourcepilot/contracts/version.py` 与本文档。
