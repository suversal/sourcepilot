# SourcePilot · 进度与待办

> 进度的**唯一真相源**。README 和 CLAUDE.md 只放一句话状态 + 指到这里，
> 避免三处各说各的。
>
> 计划本身（为什么这么拆、每步要解决什么）在 [采集平台开发文档.md](采集平台开发文档.md) §8；
> 契约决议在 [contract.md](contract.md) §0；每次变更的具体理由在 git commit message 里。
> 本文件只回答三个问题：**做到哪了 · 怎么验证的 · 还欠什么**。

最后更新：2026-07-25

---

## 一句话状态

契约 v1.1.0，采集链路端到端跑通（22 源 / 3 端点 / 后台定时采集），X 后端未动。
6 步落地顺序完成前 2 步。

---

## 阶段进度

| # | 阶段 | 状态 | 产出 | 验收依据 |
|---|---|---|---|---|
| 1 | 冻结工具契约 | ✅ 完成 | [contract.md](contract.md) v1.1.0、`src/sourcepilot/contracts/` | 26 项契约不变量测试（`tests/test_contracts.py`） |
| 2 | REST + SKILL.md + 首个信源 | ⚠️ 基本完成 | 声明式引擎（JSON/HTML/RSS）、22 个信源、3 个端点、后台调度器、[SKILL.md](../SKILL.md) | 127 项离线测试 + 真实 uvicorn curl 验证 |
| 3 | X 后端（签名/账号池/限流） | ⬜ 未开始 | — | — |
| 4 | 可靠性层（Canary/故障转移/代理） | ⬜ 未开始 | — | — |
| 5 | MCP 出口 | ⬜ 未开始 | — | — |
| 6 | 迁 RSS + 公众号 channel | ⬜ 未开始 | — | — |

第 2 步标 ⚠️ 而非 ✅ 的原因：**SKILL.md 从未在真实 Agent 上跑过**，
「提问→查→中文简报」整条链路还只是纸面设计。见下方待办 T1。

### 已上线

- `GET /api/v1/hotlist` — 多平台热榜（缓存），单平台失败不拖垮全局
- `GET /api/v1/items` — 归一化信息流，喂 AIRADAR，带 `since` 增量 + cursor 分页
- `GET /api/v1/health` — 分源采集状态（Canary 做起来之前唯一的可观测窗口）
- `GET /api/v1/article` — 读单篇正文转 Markdown（现查，带 SSRF 防护）

已接信源 22 个，分两类：

- **厂商官方发布**（`source=vendor`，8 个）：OpenAI · Anthropic · DeepSeek ·
  智谱 GLM · Kimi · 通义千问 · 字节 Seed · Google AI
- **平台热榜**（`source=hotlist`，14 个，newsnow 科技分类全量）：B站 · 头条 ·
  V2EX · 掘金 · 少数派 · LINUX DO · AIHOT · 36氪 · GitHub · HN · IT之家 ·
  Solidot · Product Hunt · 远景论坛

引擎能力：JSON / HTML(CSS 选择器) / RSS 三种提取器、`impersonate` TLS 指纹伪装、
`strptime` 人类可读日期、`slug` 标题转地址、`verify_urls` 推导地址自检、
`exclude_if` 关键词剔除、`pre_request` 访客 cookie。

**后台调度器**（T2 已完成）：每 60s 检查一次，到点的源自动采集。没有它，只有被
`/hotlist` 请求打到的源会更新，厂商发布那类只走 `/items`，库里会永远是空的。

性能：冷启动约 40s（22 源串行抓），查询路径不做网络请求，稳定在毫秒级。
实测一轮采集入库 1693 条。

### 契约里定义但尚未实现

`search_x` · `get_x_timeline` · `get_wechat_feed`

**刻意不给占位端点**——没接的能力就是访问不到，不返回假数据。
SKILL.md 里也写明让 Agent 如实说「这个信源还没接」。

---

## 待办

按「拦路程度」排，不是按工作量。

| # | 事项 | 为什么重要 |
|---|---|---|
| T1 | 用 Codex 装 SKILL.md 实测整链路 | 现在便宜，等 X 写完再回头改 SKILL.md 要贵得多 |

| T3 | X 后端签名可行性验证 | 第 3 步的全部价值都押在这上面。动手写之前先确认 `x-client-transaction-id` 那套现在还有效 |
| T4 | 跨源去重 | 契约说按 url 规范化 + 标题相似度归并。目前**只做了 url 规范化，归并逻辑没写**，跨源重复条目会直接进 feed |
| T5 | 并发抓取 | 22 源串行，冷启动约 40s。源越多越难看，且调度器一轮要跑很久 |
| T6 | 数据清理策略 | `items` 表只增不删，跑久了会一直长 |

---

## 已知问题

写在这里的都是**实测过的结论**，不是猜测。

| 问题 | 实测情况（2026-07-25） | 现在怎么处理 |
|---|---|---|
| 微博热搜要 cookie | 不带 cookie 直接 `403 {"error":"Forbidden"}` | 配置留在仓库但 `enabled: false`，理由写在 [weibo.yaml](../config/sources/weibo.yaml) 文件头。等 Canary 能发现 cookie 失效后再启用 |
| 抖音「先领访客 cookie」失效 | `pre_request` 拿不到任何 cookie，热搜接口返回空字符串，现已需签名 | 没做进去。`pre_request` 配置字段保留但**当前无源使用**，尚未被真实验证过 |
| 知乎热榜要鉴权 | `401 AuthenticationError` | 没做进去 |
| LINUX DO 挂在 Cloudflare 后 | 换 UA、补全套浏览器头、先取 cookie 全部 403「Just a moment...」；`impersonate=chrome/chrome131` 仍被拦，**`safari` 能过** | 配置 `request.impersonate: safari`。对方调策略时改这一行 |
| 酷安要签名请求头 | newsnow 用设备参数 + token 算 `X-App-Token` | 配置留在仓库但禁用。签名属「重逻辑单写」，等 X 后端把那套基础设施做出来后统一接 |
| Product Hunt 官方 API 要 Key | newsnow 走 GraphQL 需 `PRODUCTHUNT_API_TOKEN` | 用它的公开 RSS（也是 newsnow 的降级路径）。本平台匿名只读，不索要 Key |
| 字节 Seed 页面里没有文章链接 | 卡片是 `div` 不是 `<a>`，跳转由 JS 处理；渲染完 DOM 里也依然没有 href。但**卡片内容本身是服务端渲染的**（外层 `display:none`），标题/日期/分类静态就能拿到 | 文章地址 = 标题 slug 化（9 条全量验证 9/9 命中）。因为这是对站点的假设，开 `verify_urls` 逐条校验兜底。**没有用浏览器自动化** |
| 智谱官网也是客户端渲染 | `zhipuai.cn/news` 抓不到条目 | 改抓开放平台文档站的「新品发布」页，每条公告的 `div.update` id 就是发布日期 |
| Anthropic 类名是构建期哈希 | `FeaturedGrid-module-scss-module__W1FydW__title` 这种，改版必变 | 选择器只依赖 href 前缀、标签结构和 `[class*="title"]` 后缀 |
| 关键词分类误标率高 | 开着时 1737 条里 model 命中 1251、product 1203，几乎等于没过滤。子串匹配让「ChatGPT」命中 `model`，匹配摘要让 RSS 随口一提就中标 | **默认关闭**，只保留主题单一信源的源级映射（model 1251→75）。空数组是诚实的，错标签会误导 AIRADAR 的筛选 |
| AIHOT 是二手聚合源 | 全仓库唯一一个吃「别人聚合结果」的源，其余 23 个都直连平台自己的接口 | 已在配置文件头标注。它挂了我们查不出根因，且内容可能与自接的一手源重复——跨源去重做出来后要留意 |
| 部分源拿不到发布时间 | 头条热榜 API、掘金热榜 API 均不返回时间字段（掘金 `ctime`/`mtime` 都是 0）；36氪快讯列表只有相对时间 | 如实标 `time_basis=discovered`。要拿真实时间得进详情页，属 `read_article` 的范畴 |
| 无代理支持 | Clash 三级优先级（per-source > 全局 > 环境变量）未接 | 抓 X 之前必须补上 |

---

## 关键决策

| 决策 | 定于 | 理由 / 出处 |
|---|---|---|
| Python + FastAPI + SQLite | 2026-07-25 | X 签名（twscrape）、公众号（we-mp-rss）、国内平台（MediaCrawler）三块最重的参考实现都在 Python 生态 |
| 首个信源选 hotlist 而非 X | 2026-07-25 | 低风险、无需账号，能验证声明式引擎；X 硬骨头留到链路稳了再单独攻 |
| 契约 6 处修订 | 2026-07-25 | 见 [contract.md](contract.md) §0 |
| 业务判断放 services.py，api.py 只做协议翻译 | 2026-07-25 | 补 MCP 出口时是加一层壳，不是抄一遍逻辑——「三出口一套核心」的守法关键 |
| 取值层不用 jsonpath | 2026-07-25 | 点分路径 + 数组下标 + 模板拼接够热榜用，不提前上依赖。表达力不够时再换 |
| 未实现的工具不给占位端点 | 2026-07-25 | 访问不到好过给假数据，也避免 Agent 拿占位响应编简报 |
| 信息流按发布时间排序与过滤 | 2026-07-25 | 原先 `window` 按收录时间过滤，首次采集会把陈年旧文全变成「今天的新闻」（OpenAI RSS 的 1050 篇历史文章挤满 24h 窗口）。改为 `window` 看发布时间、`since` 看收录时间，各管各的 |
| 字节 Seed 不上浏览器自动化 | 2026-07-25 | 先验证了「文章地址 = 标题 slug 化」9/9 命中，静态请求就能拿到完整博客流。符合「能走稳定接口就不用浏览器自动化」 |
| 新增 `vendor` 源类型（契约 1.1.0） | 2026-07-25 | 按「谁发的」而非「怎么抓的」分类。同一厂商可能今天有 RSS、明天只剩 HTML，下游不该因传输方式变了就得改查询 |
| 厂商发布不进 `/hotlist` | 2026-07-25 | 热榜是「大家在讨论什么」，厂商发布是「官方说了什么」。混在一起会让热度排序失去意义 |
| 查询路径不触发抓取 | 2026-07-25 | `/items` 纯读库，由后台调度器填。AIRADAR 每次拉数据都该是毫秒级，不能被上游抖动拖住 |
| 接入 newsnow 科技分类全量 | 2026-07-25 | 为此给引擎补了 HTML 与 RSS 提取器——「新增源=改配置」只有在引擎覆盖信源实际用的格式时才成立 |
| 反爬手段做成配置而非代码 | 2026-07-25 | `impersonate`、`pre_request`、`exclude_if` 都是配置字段。对方改策略时改一行 YAML，不动逻辑 |

---

## 变更节点

| 提交 | 内容 |
|---|---|
| `9bc8ba5` | 契约层 v1.0.0：冻结 Item / 信封 / 错误码 / 六工具入参 |
| `2f945ba` | 声明式热榜引擎 + REST 出口 + SKILL.md |
| `63e5c54` | 补进度文档，收拢三处漂移的状态记录 |
| `ad05650` | 接入 newsnow 科技分类全量：引擎补 HTML/RSS 提取器 + TLS 指纹伪装，信源 4 → 14 |
| `7f35ad6` | 接入 8 家 AI 厂商官方发布；契约加 `vendor` 类型升 1.1.0；补后台调度器（T2） |
| 本次 | 字节 Seed 换成真实博客流（slug 推导 + URL 自检）；修正信息流的时间语义 |
