# SourcePilot

面向 Agent 的弹性信息采集平台：把异构、有反爬、随时会变的信源，
转成稳定、归一化、可被 Agent 与程序调用的服务。

职责只有**看见 · 抓取 · 归一化**。排序、LLM 分析、面向用户的推送由下游负责——
这条边界贯穿全部设计，下文会反复提到它为什么重要。

```
35 个信源 · 6 个工具 · 4 个出口 · 536 项测试
```

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [核心能力](#核心能力)
- [快速开始](#快速开始)
- [信源清单](#信源清单)
- [抓取方案：四层反爬](#抓取方案四层反爬)
- [声明式源引擎](#声明式源引擎)
- [可靠性设计](#可靠性设计)
- [接入方式](#接入方式) — 速查；完整的调用方文档见 [docs/接入指南.md](docs/接入指南.md)
- [数据模型](#数据模型)
- [架构](#架构)
- [几个刻意的设计决定](#几个刻意的设计决定)
- [已知边界](#已知边界)
- [开发](#开发)

---

## 它解决什么问题

想知道「AI 圈今天发生了什么」，你得同时盯着：厂商官网、国内外热榜、微信公众号、
以及 X 上的实时讨论。每个源的接口、反爬、数据形状都不一样，而且**随时会变**。

SourcePilot 把这些差异吃掉，对外只暴露一套统一的条目 schema 和六个工具。
信源改版时改的是配置，不是调用方的代码。

与只读自家缓存库的同类服务相比，关键差异是**能在提问那一刻现场搜 X**——
其余能力都是「预采集 + 查缓存」，这一条是真正的实时查询。

## 核心能力

| 工具 | 说明 | 取数 |
|---|---|---|
| `search_x` | **现场搜索 X**，任意关键词 | 现查 + 缓存兜底 |
| `get_x_timeline` | 指定账号的推文时间线 | 现查 + 缓存兜底 |
| `get_hotlist` | 多平台科技热榜 | 缓存 |
| `get_wechat_feed` | 订阅公众号的最新文章 | 缓存 |
| `get_feed` | 归一化资讯流，支持关键词检索与多维过滤 | 缓存 |
| `read_article` | 抓取指定 URL 正文并转 Markdown | 现查 |

六个工具通过 **REST / MCP / SKILL.md / RSS** 四个出口暴露，共用同一套服务层——
不是四份实现，是一套核心加四个协议壳。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m uvicorn sourcepilot.api:app --app-dir src --port 8420
```

```bash
curl -s "http://127.0.0.1:8420/api/v1/items?source=vendor&window=30d&limit=5" | python3 -m json.tool
```

交互式 API 文档在 `/docs`，可以直接点着试。
启动后调度器会在后台按各源自己的节奏采集，无需手动触发。

## 信源清单

35 个启用源。条目数是库里的实测值（截至 2026-08-18，共 25100 条）：

### 厂商官方发布（`source=vendor`，一手信息）

| 源 | 抓取方式 | 条目 | 最近入库 |
|---|---|---|---|
| OpenAI | RSS | 1094 | 08-17 |
| NVIDIA 开发者 | RSS | 122 | 08-17 |
| Hugging Face | RSS | 118 | 08-17 |
| Google DeepMind | RSS | 107 | 08-13 |
| AWS 机器学习 | RSS | 73 | 08-17 |
| Cursor 更新日志 | RSS | 55 | 08-17 |
| NVIDIA | RSS | 37 | 08-17 |
| Google AI | RSS | 29 | 08-17 |
| Kimi（月之暗面） | HTML | 28 | 08-17 |
| 智谱 GLM | HTML（开放平台更新日志） | 24 | 07-25 |
| GitHub AI & ML | RSS | 19 | 08-17 |
| Anthropic | HTML（/news 列表页） | 18 | 08-14 |
| DeepSeek | HTML（api-docs 侧栏） | 16 | 08-13 |
| GitHub 工程博客 | RSS | 14 | 08-10 |
| 字节 Seed | HTML + 标题 slug 推导 | 10 | 08-05 |

厂商发布是低频源，「最近入库」隔几天很正常——它反映的是对方发不发，不是我们抓不抓。
真正的采集健康看 `/api/v1/health`。

### 平台热榜（`source=hotlist`）

| 源 | 抓取方式 | 条目 |
|---|---|---|
| 今日头条 | JSON | 5227 |
| AIHOT | JSON | 4894 |
| IT之家 | RSS | 3937 |
| Hacker News | JSON（Algolia 官方 API） | 1607 |
| LINUX DO | JSON（**需 TLS 指纹伪装**） | 1446 |
| B站排行榜 | JSON | 1118 |
| 掘金 | JSON | 639 |
| Product Hunt | RSS | 468 |
| 远景论坛 | RSS | 387 |
| V2EX | JSON | 371 |
| Solidot | RSS | 215 |
| GitHub Trending | HTML | 164 |
| 少数派 | JSON | 64 |

### 媒体（`source=rss`）

| 源 | 条目 |
|---|---|
| InfoQ 中国 | 291 |
| 爱范儿 | 192 |
| TechCrunch AI | 183 |
| 量子位官网 | 166 |
| Latent Space | 42 |

### 微信公众号（`source=wechat`）

**24 个号：20 个国产大模型厂商官方号 + 3 个大厂技术号 + 1 个媒体。** 这条线的价值在于：
国产厂商的官网普遍是 SPA 且几乎全都没有 RSS（实测 MiniMax、百川、
阶跃星辰、零一万物、面壁、腾讯混元、百度、讯飞无一例外），
**一手发布实际走的就是公众号**。

智谱清言 · 智谱 · GLM大模型 · Kimi开放平台 · 月之暗面 Kimi · 千问大模型 ·
千问AI平台 · 通义实验室 · MiniMax 稀宇科技 · MiniMax开放平台 · 百川智能 ·
字节跳动Seed · 火山引擎 · 豆包 · 腾讯混元 · 百度文心 · 百灵大模型 ·
讯飞开放平台 · DeepSeek · 智源研究院 · 京东技术 · 京东云开发者 · 小米技术 · 机器之心

> **当前状态：这条线被微信读书的人机验证挡着，最近一次成功入库是 2026-08-08。**
> 自检（`python -m sourcepilot.channels.wechat.weread_check`）现在停在 `CAPTCHA`：
> 书架接口与阅读器页通行证都正常，24 个号也都能定位到 bookId，卡在真正拉文章那一步。
> 根因是出口 IP 被腾讯风控标记（排查过程见 [docs/progress.md](docs/progress.md)），
> 不是凭据或代码问题。**其余 34 个源不受影响**——公众号 channel 是隔离的，
> 这正是「一个源崩了不许拖垮全局」那条铁律要的效果。

账号全部写死 `fakeid` 而不是按名字搜，这是**正确性问题不是优化**——
实测搜「智谱AI」命中的是个 2022 年就停更的同名号，搜「Kimi」命中的是
2018 年一个讲电影票的无关号。而搜索正是公众平台上最容易触发风控的动作（每轮 24 次而不是 48 次）。

查号用 `python -m sourcepilot.channels.wechat.lookup <名字或微信号>`，
它会把每个候选的**最近更新日期**一并列出来——挑号要看活跃度，不能看名字像不像。
**用微信号搜最准**：它全平台唯一且不可改，搜索会按它精确匹配（配置里的
`alias` 字段就是记它，不参与请求，是 fakeid 失效时的溯源凭据）。

**分批轮转**：每轮只抓 6 个号，四轮覆盖 24 个。这是应对风控的主要手段——
实测微信读书看的是**单轮请求总量**而不是瞬时密度：24 个号一次打完会弹人机验证，
而把账号间隔从 3 秒放到 8 秒并不管用（第 1 个号就被弹）。摊成 4 轮才落在容忍度内，
代价是单个号的更新延迟从 6 小时变 24 小时。

走公众平台后台接口，需自行配置凭据（见 [wechat.yaml](config/sources/wechat.yaml) 文件头）。

凭据自检：`python -m sourcepilot.channels.wechat.check`。它**两个接口都打**——
公众平台按接口分别限流，实测出现过 `searchbiz` 返回 `ret: 0` 而 `appmsg`
同时是 `200013 freq control` 的情况。只验证搜索接口会得出「凭据没问题」的
错误结论，而采集走的恰恰是 `appmsg`。

### X / Twitter（`source=x`）

时间线定时采集 + 按需现场搜索。

### 未启用（配置在仓库里，禁用理由写在各自文件头）

- **微博** — 不带 cookie 直接 403，等 Canary 能发现 cookie 失效后再启用
- **酷安** — 需要设备参数算 `X-App-Token`，签名属重逻辑范畴
- **36氪** — 2026-08-07 整站挂到了火山引擎的安全检测后面。`/feed` 回的是一张挑战页
  （HTTP 200、17KB、零个 `<item>`），curl_cffi 的五种指纹全部穿不过去——那道墙要执行 JS。
  停用理由与试过的路子写在 [36kr.yaml](config/sources/36kr.yaml) 文件头
- **11 个媒体/研究/社区 RSS** — BAIR、微软研究院、MIT 科技评论、Simon Willison、
  Smol AI、The Decoder、The Verge、VentureBeat、TLDR AI、两个 Reddit 板块。
  源本身实测可用且字段完整，关掉是为了**与 AIRADAR 的实际启用清单对齐**——
  它库里这些是 `is_active=false`。要恢复只需改一行 `enabled`
- **通义千问 Qwen** — 2026-08-18 停用。`qwenlm.github.io` 还能抓 44 条、格式完整，
  但最新一篇是 2025-09-22：Qwen 的博客搬到了 qwen.ai（SPA，`/rss.xml` 返回外壳 HTML）。
  **抓得到旧内容比抓不到更危险**——采集成功、Canary 正常、下游却会以为「Qwen 最近没发布」。
  复开要找 qwen.ai 的列表 API，属重逻辑而非改配置
- **搜狗微信** — 实测数据陈旧（量子位只出 2 条且含 2019 年的、机器之心 9 条全是 2017 年），
  且约 20 次请求就触发验证码。**一个静默返回旧文的兜底比没有兜底更危险**

## 抓取方案：四层反爬

按「能少用就少用」的原则分层，每一层只在需要时才开——每层都是维护成本。

### 第一层 · 请求指纹

伪造 User-Agent、补齐 Referer 与客户端提示头。X 账号可携带**签发该 cookie 的
那个浏览器的完整指纹**——cookie 是 Chrome 150 签发的、请求却报称 Chrome 131，
这种自相矛盾本身就是风控信号。

### 第二层 · TLS/JA3 指纹伪装

某些站点（如 LINUX DO）挂在 Cloudflare 后面，返回「Just a moment...」挑战页。
**它拦的是 TLS 握手指纹，改 UA 或补请求头都没用**。用 `curl_cffi` 在 TLS 层伪装。

实测记录：`impersonate=chrome` 和 `chrome131` 仍被 403，**`safari` 能过**。
这类结论写在配置注释里，对方调策略时改一行即可。

### 第三层 · 动态签名（X 专用）

X 对搜索强制要求 `x-client-transaction-id`。这个签名的生成方式相当迂回：

```
① 从登录态页面取 verification key（48 字节，每次请求都不同）
② 从加载动画的 SVG 贝塞尔路径取动画帧
③ 从动态 chunk（ondemand.s / sign.o）取动画索引
④ 三次贝塞尔插值 + 2D 旋转矩阵 → anim_key
⑤ SHA256(方法!路径!时间戳+关键字+anim_key)
⑥ 拼 vk_bytes + 时间戳字节 + 哈希前 16 字节 + 尾字节
⑦ 随机字节 XOR 混淆 → base64
```

绕这么大圈的用意是：**光有 cookie 不够，你还得真的解析过它的前端**。

实测确认的几件事：

- 签名**只对搜索强制**。`UserByScreenName` / `UserTweets` / `UserMedia` 不带签名一律 200，
  `SearchTimeline` 不带就是 404
- 签名**一次性**。截获浏览器刚生成的签名原样重放，依然 404
- 密钥**必须从登录态页面解析**。匿名访问拿到的是 `entry-client-logged-out-*.js` 入口，
  那个 bundle 里根本没有签名脚本（匿名 35KB / 1 chunk vs 登录 271KB / 3 chunk）
- 密钥**会随 X 发版失效**，所以 404 时会自动重取一次再重试，另有 TTL 兜底

验证方式不是「跑通了就算」：独立用 JS 重写一遍 anim_key 计算，与 Python 版在两组
真实输入上逐字符比对一致，再用生成的签名打真实端点拿到 200 / 133KB / 20 条推文。

### 第四层 · 账号池与限流状态机

核心是**两件事必须分开判断**：

| 信号 | 处理 | 账号状态 |
|---|---|---|
| `x-rate-limit-remaining == 0` | 锁到 reset 时间，换下一个账号 | 仍有效 |
| 错误码 32/64/88/89/326、HTML + `cf-ray` | 永久停用 | 废了 |

搞混的代价**不对称**：把「废了」当「限流」会让你拿一个已封账号反复去撞，加速关联封号；
把「限流」当「废了」只是白白少一个账号。所以判断从严。

判断**顺序**也有讲究——先看是不是废了，再看是不是限流。反过来的话，一个被封的账号
会因为限流头恰好正常而被当成健康账号继续用。

限流按 **endpoint 分别记**：搜索被限不代表时间线也被限。

### 多后端降级

X 的三个后端按能力分工，不是简单一条链从头试到尾：

| 能力 | 后端顺序 | 认证 |
|---|---|---|
| 搜索 | GraphQL | 必须登录 + 签名 |
| 时间线 | Nitter → GraphQL | Nitter 零认证 |
| 单推 / 资料 | FxTwitter | 零认证 |

时间线把零认证的 Nitter 排在前面是刻意的：**账号是稀缺且脆弱的资源，能不动用就不动用**。

### 公众号正文：已知容器专用提取

`read_article` 默认走 trafilatura——它擅长从一整个页面里**猜**正文在哪。
代价是激进降噪：实测公众号文章正文提得很干净（5360 字），但 **6 张配图和
4 个小标题全被当噪音丢了**，给它还原 `data-src` 也没用，被丢弃的是节点本身。

所以分工是：**容器已知时按标签逐个翻译，容器未知时才交给 trafilatura**。
公众号正文恒在 `#js_content` 里（这个 id 多年没变），既然不需要猜，就不必
承担猜错的代价。

同一篇文章的对比：

| | trafilatura | 专用提取 |
|---|---|---|
| 正文 | 5360 字 | 6704 字 |
| 配图 | 0 张 | **6 张** |
| 小标题 | 0 个 | **7 个** |
| 加粗 | 36 处 | 36 处 |

两个坑值得记（都是实测撞出来的）：

- 公众号的图**只有 `data-src`**，`src` 属性根本不存在——只认 `src` 一张也拿不到。
- 4 个 `<h2>` 的文字全包在 `<span>` 里。按「纯容器就下钻」处理的话文字出来了、
  `##` 前缀却丢在半路，**整篇正文一个小标题都没有**。标题标签必须当一整块处理。

新增站点只要在 `SITE_CONTAINERS` 里加一行域名 → 选择器。选不中时自动回落
trafilatura，所以对方改版最坏也只是退回原来的质量。

### X 长文（Articles）

X 的长文正在成为一种主流的深度内容形式，但**搜索与时间线接口只给约 100 字预览**
——对下游等于没拿到。平台会为带长文的推文单独再取一次全文。

这条路不好找：正文藏在 `TweetResultByRestId` 的 `withArticleRichContentState`
开关后面，而那个 fieldToggle 默认是关的。前端 bundle 里也挖不到（相关代码在
按需加载的 chunk 里），最后是**从浏览器的实际网络请求里截获的**。

正文格式是 Draft.js 的 `{blocks, entityMap}`，转成 Markdown 后保留二级标题、
链接与配图。X 同时提供 `plain_text`，但那一版把标题和正文拍平、链接只剩锚文本，
所以只作解析失败时的兜底。

```bash
curl "http://127.0.0.1:8420/api/v1/x/tweets?has_article=true"
```

## 采集节流

三层，各管各的：

| 手段 | 省什么 | 实测收益 |
|---|---|---|
| `min_interval` | 请求次数 | **最大**。按各源真实产出速度重设后，2640 → 2160 次/天 |
| 条件请求（ETag / Last-Modified） | 传输 + 解析 + 入库 | 命中 304 时全省，但只有少数源支持 |
| `max_items` | 解析与入库量 | 有限，见下 |

**频率是大头。** 8 个厂商源曾经每 15–30 分钟抓一次，实测一整天产出 0 条——
官方博客一周才发几篇。改成 1 小时后 vendor 类从 456 次/天降到 192 次。

**条件请求省的是带宽不是时间。** 带上上次的 ETag 再请求，对方没变就回 304，
正文一个字节不传。实测 Qwen：38KB → 0KB，耗时 1273ms → 727ms。
但 RTT 才是耗时主导，所以对小文件时间收益有限。26 个源里只有 Qwen 支持
——OpenAI、Google AI、Solidot、B站都不发 ETag。

**`max_items` 的收益比直觉小。** OpenAI 的 RSS 一次吐 1050 篇十年历史，
截断到 100 条后解析从 198ms 降到 150ms——因为 feedparser 得先解析完整个
XML 才能给出条目列表，截断只能省最后一步逐条建 Item。它真正挡住的是
入库写放大和内存占用，不是解析时间。

接新源想一次收全历史时，把 `max_items` 设成 `null`。

## 声明式源引擎

新增一个热榜源 = 写个 YAML 丢进 `config/sources/`，**不用改代码**。三种格式：

**JSON** — 点分路径取值，`{}` 模板拼 URL：

```yaml
name: example
display_name: 示例热榜
platform: example
min_interval: 300     # 抓取间隔下限，按该源的实际内容产出速度定
max_items: 100        # 单次最多取列表前 N 条，null = 不限（默认 100）
ranked: true          # 这是排行榜，score 由榜内位置换算
request:
  url: https://example.com/api/hot
status:               # 站点用 HTTP 200 + 体内错误码表示拒绝时声明
  path: code
  ok: [0]
  message_path: message
  rate_limited: [-352]
extract:
  list: data.list     # 列表在 JSON 里的位置，留空表示根即列表
  fields:
    native_id: id
    title: title
    url: { template: "https://example.com/p/{id}" }
    published_at: { path: ctime, type: unix }
```

**HTML** — `list` 是行的 CSS 选择器，字段用 `select` 取文本或属性：

```yaml
base_url: https://example.com    # 相对链接自动拼成绝对链接
extract:
  format: html
  list: "#list > ul > li"
  fields:
    native_id: { select: "a.t", attr: href }
    title: { select: "a.t" }
    url: { select: "a.t", attr: href }
    published_at:
      select: "time"
      pattern: '^(\d{4}-\d{2}-\d{2})'   # 先按正则抽一段再转类型
      type: iso
  exclude_if:
    title: [优惠, 补贴]                  # 剔列表里的推广位
```

**RSS** — 条目形状固定，`fields` 整个可以不写：

```yaml
extract:
  format: rss
```

### 字段取值能力

| 能力 | 说明 |
|---|---|
| `path` | JSON 点分路径 + 数组下标 |
| `select` / `attr` | CSS 选择器取文本或属性 |
| `template` | 用已抽出的字段拼接，支持 `\|urlencode` |
| `pattern` | 取值后按正则抽第一个捕获组，再转类型 |
| `type` | `str` `int` `float` `unix` `iso` `strptime` `slug` |
| `format` | 配合 `strptime` 解析人类可读日期（`Jul 9, 2026`） |
| `exclude_if` | 按关键词剔除条目 |
| `verify_urls` | URL 是推导出来时逐条 HEAD 校验，404 的丢掉 |

`slug` 那个类型有个真实用例：字节 Seed 的博客卡片是 `div` 不是 `<a>`，
**页面里根本没有文章链接**，但文章地址正好是标题的 slug 化结果——
9 条全量验证 9/9 命中。因为这是对站点的假设，所以配合 `verify_urls`
把「假设失效」变成可见的条目数下降，而不是悄悄产出一堆死链。

### 重逻辑 channel

需要登录态、签名或账号池的源（X、公众号）单独写 Python，但**仍走同一套调度、
状态记录与降级路径**——出口层看到的是同一种结果，不因为后端不同而分叉。

## 可靠性设计

### Canary 自检

35 个源里任何一个改版、被封或返回空，如果只在日志里留一行 warning，
等发现时可能已经断了好几天。Canary 做三级判定：

| 判定 | 触发条件 |
|---|---|
| `down` | 连续失败 ≥3 次 |
| `degraded` | 落后超过**自身间隔的 3 倍**、采集成功但零条目、近期有失败 |
| `idle` | 未启用或刚启动还没轮到 |

两个刻意的设计：

**落后按源自己的间隔算倍数**，不用统一阈值——1 小时抓一次和 5 分钟抓一次的源，
「落后」的定义本就不同；固定阈值会把慢源全报红。

**一两次失败不判 down**——那多半是网络抖动，为此报警会训练人忽略告警，
**那比没有告警更糟**。

「采集成功但零条目」单独判 degraded：选择器半坏比整个挂掉更隐蔽。

结果暴露在 `/api/v1/health`：

```json
{
  "ok": true,
  "canary": { "counts": {"ok": 32, "degraded": 1, "down": 2, "idle": 15}, "problems": [] }
}
```

### 采集中断告警

Canary 判得再准也有一个前提：**得有人去看**。2026-08-08 公众号线因为出口 IP
被风控而停掉，到 08-17 才被发现——中间 9 天里 `/health` 每一次都如实报着 `down`。
**一个源坏掉是必然的，9 天发现不了才是真问题。**

所以加了一层主动推送（Telegram）：

```bash
export TELEGRAM_BOT_TOKEN=...   # 与 AIRADAR 的 telegram_notifier 同名，同一个机器人可复用
export TELEGRAM_CHAT_ID=...
python -m sourcepilot.alert --test    # 先验通道
python -m sourcepilot.alert           # 检查一次并按需推送（也可挂 cron 兜底）
```

配了这两个变量，API 进程的调度器每轮采集后自动检查；没配就整段跳过，其余功能不受影响。

```
🛰 SourcePilot 采集告警

❌ wechat：连续失败 39 次（最后一次：CAPTCHA）
   上次成功 08-07 14:57Z（10.5 天前）

✅ 已恢复：bilibili

35 个源：ok 32 · degraded 1 · down 2
```

三条刻意的约定：

**只在状态转换时发**。`→ down` 一条，`down →` 恢复一条。一个源一直坏着不会每分钟
吵一次；`degraded` 根本不发——落后几分钟、条目数掉一半这类波动太频繁，
**告警一吵人就不看了，那等于回到没有告警**。

**已推送状态存库**（`alert_state` 表）而不是存内存，否则每次重启都会把同一批
陈年故障重推一遍。

**推送失败不更新状态**，下一轮自然重试。反过来做（先记已通知、再发送）会让一次
网络抖动永久吞掉一条告警——而告警恰恰是出问题时才用的东西，那时候网络本来更可能不好。

发送本身是 best-effort：绝不抛异常、绝不阻塞采集。告警挂掉是小事，把调度线程带崩是大事。

### 业务错误码识别

很多站点**永远回 HTTP 200**，真实结果藏在响应体里。不识别的话，
「被风控挡了一下」会一路走到提取层、表现为「取不到列表」，然后被报成
「多半是对方改版了」——那句话会把排查方向完全带偏。

实测案例：B站限流时返回 `HTTP 200 / code=-352 / data=null`。
声明 `status` 规则后正确报 `RATE_LIMITED`，冷却状态机会退避。

### 冷却状态机

区分「临时挡一下」和「这条路废了」，冷却时长分档：

```
AUTH_EXPIRED   6 小时    重试没意义，等人换凭据
RATE_LIMITED   30 分钟   对方在说「慢点」
CAPTCHA        30 分钟
其余           不冷却    改版/网络抖动是局部问题，冷却整体会饿死其它账号
```

**冷却状态落盘**。只放进程内的话，重启一次就清零——真被封号时重启一下就又去捅了。
那是账号安全问题，不是体验问题。

### 分级保留策略

不能一刀切按时间删——热榜是**快照**（「今天 B站第 3 名」一周后毫无价值），
厂商发布是**一手资料**（三年前的发布说明今天检索照样有用）：

| 类型 | 保留 |
|---|---|
| hotlist | 90 天 |
| x | 30 天 |
| wechat | 365 天 |
| vendor | 永久 |

判定按发布时间而非收录时间——按收录时间判的话，一篇今天才被发现的旧文会被立刻删掉。

### 现查降级链

`search_x` 是唯一的现查工具，降级语义写死在契约里：

```
live=true  → 现查成功        → mode=live  stale=false
             现查失败+缓存有 → mode=cache stale=true  ok=true   ← 降级不是错误
             现查失败+缓存空 → ok=false，报原始错误码
live=false                   → mode=cache stale=false           ← 用户要缓存，不算降级
```

参数错误**不降级**——缓存里没有「用户打错的那个词」的结果，返回一堆不相干的旧数据
比报错更糟。现查到的结果顺手入库，这是下次降级有东西可用的前提。

## 接入方式

> 下面是速查。**要接入的话看 [docs/接入指南.md](docs/接入指南.md)**——那份是写给调用方的
> 完整文档：信封与错误码怎么读、Item 各字段的坑、分页与增量同步、RSS 与 MCP 怎么配、
> 以及接入前必须知道的四条边界。

### 一、REST（给程序）

```bash
# 各家 AI 厂商近 30 天的官方发布
curl "http://127.0.0.1:8420/api/v1/items?source=vendor&window=30d"

# 只要指定的几个信源（逗号分隔）
curl "http://127.0.0.1:8420/api/v1/items?platform=bilibili,toutiao,juejin"

# 关键词检索（中文直接按字符匹配，无需分词）
curl "http://127.0.0.1:8420/api/v1/items?q=智谱&window=all"

# 现场搜 X
curl "http://127.0.0.1:8420/api/v1/x/search?q=Claude+Opus+5"

# 读某篇文章正文
curl -G "http://127.0.0.1:8420/api/v1/article" --data-urlencode "url=https://..."
```

**增量拉取**：首次带 `since`（上次的时间戳），翻页带 `cursor` 并保持 `since` 不变。
实测拉取近 10 分钟新增只要 4ms，轮询成本几乎为零，且天然幂等——
消费方挂了重启带上次的 `since` 继续，一条不丢。

### 二、MCP（给 AI 客户端）

```bash
.venv/bin/python -m sourcepilot.mcp_server
```

Claude Desktop 之类的客户端配置：

```json
{
  "mcpServers": {
    "sourcepilot": {
      "command": "/绝对路径/.venv/bin/python",
      "args": ["-m", "sourcepilot.mcp_server"],
      "env": { "PYTHONPATH": "/绝对路径/src" }
    }
  }
}
```

`env` 里的 `PYTHONPATH` 不能省——MCP 客户端会清理环境变量。

MCP 的 tool schema 由契约的 pydantic 模型直接生成，**参数定义只有一份**，
REST 与 MCP 不可能对不上（有专门的一致性对照测试）。

### 三、RSS（给阅读器与自动化工具）

```
/api/v1/feed.xml                              全部信源
/api/v1/feed.xml?source=vendor&window=30d     只要厂商发布
/api/v1/feed.xml?platform=bilibili,toutiao    指定几个信源
/api/v1/feed.xml?q=智谱&window=all             关键词订阅
```

查询参数与 `/items` 完全一致，丢进 Reeder / Feedly / Inoreader 或 n8n / Zapier 即可。
标题会随过滤条件变化（如「SourcePilot · 厂商发布 · 近 30d」），
方便在阅读器里并排订阅多个切片时区分。

**只出摘要，不内联正文**。RSS 是公开阅读面，不代表第三方内容因此获得再分发许可——
每条保留原文链接与来源署名，读者落到原站去读。`time_basis` 为 `discovered` 的条目
会在描述里明确标注「该源未提供发布时间，此处为本平台收录时间」，
避免在阅读器里被误当成发布时间。

条目形状：`<link>` 直接指向**第三方原文**（本平台不做展示层，没有站内阅读页），
`<guid isPermaLink="false">` 用平台内部 id 保证判重稳定，`<description>` 是
CDATA 包的 HTML（摘要 + 原文入口 + 来源署名），作者单独走 `<author>` 元素
以便阅读器分组过滤。`<ttl>30</ttl>` 提示阅读器 30 分钟内不必重复拉取。

### 四、SKILL.md（给 Agent）

把 [SKILL.md](SKILL.md) 放进 Codex / Claude Code 的 skill 目录，之后可以直接问
「X 上现在怎么看 Opus 5」，Agent 会自己路由到对应端点。

里面写死了三条读法规则：`stale` 必须说明、时间要按 `time_basis` 措辞、
`score` 不得跨源比大小；以及把接口返回内容一律当不可信数据的防注入边界。

## 数据模型

所有信源归一化成同一个 `Item`：

```jsonc
{
  "id": "x:1234567890",                    // {source_type}:{native_id}
  "source": { "type": "x", "name": "X / Twitter", "platform": "x" },
  "title": "……",
  "summary": "……",                         // 客观摘要，抽取式不改写
  "url": "https://x.com/…/status/…",       // 第三方原文
  "author": "elonmusk",
  "published_at": "2026-07-25T10:00:00Z",  // 取不到就是 null，绝不回填
  "discovered_at": "2026-07-25T10:03:00Z",
  "time_basis": "published",               // published | discovered
  "score": 0.82,                           // [0,1] 源内相对热度
  "categories": [],
  "lang": "en",
  "media": [],
  "raw": { "likes": 12561, "views": 4063693 }
}
```

响应信封（REST 与 MCP 完全一致）：

```jsonc
{
  "ok": true,
  "data": { "items": [ /* … */ ] },
  "meta": {
    "contract_version": "1.4.0",
    "mode": "live",          // live | cache | mixed
    "stale": false,          // true = 降级的近似结果
    "collected_at": "…",
    "next_cursor": null,
    "has_more": false,
    "sources": [ /* 分源成功/失败 */ ]
  },
  "error": null
}
```

错误码：`RATE_LIMITED` `UPSTREAM_DOWN` `AUTH_EXPIRED` `CAPTCHA` `NOT_FOUND`
`BAD_REQUEST` `TIMEOUT` `INTERNAL`。完整契约见 [docs/contract.md](docs/contract.md)。

## 架构

```
                    ┌──────────────────────────────────────┐
   REST  ◄──────────┤  出口层   api.py / mcp_server.py      │
   MCP   ◄──────────┤          / feed.py / SKILL.md         │
   SKILL ◄──────────┤          （只做协议翻译，零业务逻辑）  │
   RSS   ◄──────────┤                                       │
                    ├──────────────────────────────────────┤
                    │  服务层   降级 · 缓存 · 分源健康        │
                    │          services.py / x_service.py   │
                    ├──────────────────────────────────────┤
                    │  可靠性   canary.py · cooldown.py      │
                    │          retention.py · collector.py   │
                    ├──────────────────────────────────────┤
                    │  信源层   sources/（声明式 YAML 引擎） │
                    │          channels/x · channels/wechat  │
                    ├──────────────────────────────────────┤
                    │  存储     store.py（SQLite）           │
                    └──────────────────────────────────────┘
```

**服务层是唯一的「一套核心」**。出口层无论几个，都只做协议翻译——
补新出口时改的是与 `api.py` 平级的新文件，不许把逻辑抄一份。

## 几个刻意的设计决定

**契约先于实现。** 第一步就冻结了工具 schema、Item、错误码，并逐条修订了原始设计里
6 处内部矛盾（详见 [contract.md](docs/contract.md) §0）。契约有 26 项不变量测试守着，
测试变红即意味着在破坏与消费方的合同。

**`published_at` 取不到就是 `null`，绝不回填。** 回填后下游再也分不出哪个是真的。
另设 `time_basis` 显式声明时间依据——展示层必须据此措辞。

**时间窗按发布时间，增量按收录时间。** 两者问的是不同的问题：「最近发生了什么」
vs「上次拉取之后你们又收到了什么」。混用会让首次采集把陈年旧文全变成今天的新闻。

**`score` 不保证跨源可比。** 它是源内相对热度；B站的 0.9 和 V2EX 的 0.9 没有可比性。
跨源排序是下游的职责，原始热度值保留在 `raw` 里供自行加权。
按时间倒序的源（RSS、公众号）固定 `0.0`——**把「第几个被列出来」换算成分数等于伪造热度信号**。

**跨源不去重。** 「一条新闻同时上了 8 个源」本身就是最可靠的热度信号之一，
归并会把它抹掉且下游再也补不回来。判断「这两条算不算同一件事」需要语义相似度，
本质是分析不是采集。

**分类默认不打标。** 关键词规则实测误标率太高（子串匹配让「ChatGPT」命中 `model`，
于是产品公告被打成模型发布）。**空数组是诚实的，错标签是有害的**——
下游拿它筛内容，喂错标签比不给更糟。

**没实现的工具不给占位端点。** 访问不到好过给假数据，也避免 Agent 拿占位响应编简报。

**签名 / operation-id / 混淆常量单独抽文件。** 对方改版时改的是配置不是逻辑。
这条已经被验证过一次：X 的三个 operation id 全部过期时，修复改动全部集中在
`config.py`，逻辑一行没动。

## 已知边界

诚实标注，都是实测结论：

- **搜狗微信兜不住**。数据陈旧（多为数年前的文章）、约 20 次请求触发验证码、
  按时间排序参数无效。已降为可选，默认不启用
- **字节 Seed 的文章地址是推导出来的**（标题 slug 化，9/9 验证通过），
  那是对站点 URL 规则的假设，靠 `verify_urls` 兜底
- **X 签名会随前端改版失效**。有自动重取，但如果页面结构本身变了，
  需要人改 `signature.py` 里的选择器与正则
- **公众号只覆盖已订阅的号**，不是全网搜索
- **`q=` 是子串匹配不是全文检索**。SQLite FTS5 的两种分词器对中文都不好使
  （`unicode61` 把整串中文当一个词，`trigram` 要求查询至少 3 字符），
  子串匹配对中文天然正确，代价是全表扫描——当前规模下是亚毫秒级
- **代理轮换未做**。35 源里只有 X 有 IP 层风险且请求量低，等抓取量上来再说
- **`read_article` 在 fake-ip 代理下要放行占位地址**。Clash 这类代理在 fake-ip 模式
  下不做真实 DNS，把每个域名映射到 `198.18.0.0/16`（IPv6 侧 `fdfe:dcba:9876::/48`）里的
  占位地址，而 Python 把那个段算作私网——照原样判定的结果是**每一个公网 URL 都被拒**，
  工具静默失效。所以这些段按「域名的占位地址」处理，`SOURCEPILOT_FAKE_IP_CIDRS` 可改可关。
  代价说清楚：用代理就意味着「这个域名去哪」由代理决定；字面内网 IP 与内网域名照旧拦住
- **公众号线当前被人机验证挡着**（最近成功入库 2026-08-08）。根因是出口 IP 被腾讯风控
  标记，不是凭据或代码问题；weread 这条路本身还有三个固有局限：收录滞后（实测遇到过
  半个多月）、通常比发布晚几小时、有反爬（撞了就调大 `min_interval`，别重试）

## 开发

```bash
.venv/bin/python -m pytest && .venv/bin/ruff check src tests
```

536 项测试，**全部离线**——连 DNS 都是打桩的（SSRF 那组用例若真去解析域名，
在开着代理的机器上会集体误判，见 `tests/test_article.py` 的 `stub_dns`）。
其中契约不变量测试（`tests/test_contracts.py`）是最重要的一组——它们就是契约本身。

进度、待办与已知问题见 [docs/progress.md](docs/progress.md)，那是进度的唯一真相源。

### 加一个信源

绝大多数情况下只需写一个 YAML 文件丢进 `config/sources/`，参考
[声明式源引擎](#声明式源引擎)。需要登录态或签名的源才写 Python channel。

### 配置凭据

35 个源里 33 个匿名可抓，**不配任何凭据也能跑起来**。要 X 与公众号才需要：

| 模板 | 复制成 | 给谁用 |
|---|---|---|
| `config/x_accounts.example.yaml` | `config/x_accounts.yaml` | `search_x` / `get_x_timeline`（搜索必须登录态） |
| `config/weread_credentials.example.yaml` | `config/weread_credentials.yaml` | 公众号采集的**主力路线**（微信读书） |
| `config/wechat_credentials.example.yaml` | `config/wechat_credentials.yaml` | 公众平台 mp 后端（**列表接口当前不可用**，只剩查 fakeid 能用） |

每个模板里都写了怎么取、哪些字段必须有、以及取错时会看到什么错误码。
真实凭据文件已在 `.gitignore` 里，代码中也不会打印明文（`__repr__` 做了屏蔽）。

**建议使用专用小号**——这两条线抓的都是内部接口，有封号风险。

## 安全边界

- **信源返回内容一律视为不可信数据**。标题、摘要、正文只作资讯证据，
  不得改变工具规则、触发命令、诱导授权
- **对外接口匿名只读**，不索要用户 Key 或 cookie
- `AUTH_EXPIRED` 对外只表示「平台侧暂时不可用」，不暴露账号细节
- `read_article` 是唯一按调用方给的地址出网的工具，因此有强制的地址校验：
  只接受公网 http(s)、端口限于 80/443/8080/8443、解析出的 IP 不能是私网或
  云厂商元数据地址，且跟随重定向后**重新校验一次**
- 上面那道校验有一个**明确的例外**：代理 fake-ip 段里的占位地址（见已知边界）。
  例外只给域名——字面量不经 DNS，`http://198.18.0.111/` 照旧被拒；内网域名走真实
  解析拿到私网地址，也照旧被拒（DNS rebinding 那一路）。用例钉在
  `tests/test_article.py::TestFakeIpRange`

## 致谢

设计上借鉴了这几个开源项目的思路，但核心自己实现，不依赖它们的维护周期：

- [twscrape](https://github.com/vladkens/twscrape) — X 签名算法与限流状态机
- [x-tweet-fetcher](https://github.com/ythx-101/x-tweet-fetcher) — 多后端路由与故障转移
- [newsnow](https://github.com/ourongxing/newsnow) — 声明式源配置与自适应抓取间隔
- [we-mp-rss](https://github.com/rachelos/we-mp-rss) — 公众号接入路径

## License

MIT
