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
| 3 | X 后端（签名/账号池/限流） | ✅ 完成 | 三后端路由、账号池 + 限流状态机、`x-client-transaction-id` 签名、`search_x` 现查降级链、两个 REST 端点 | 39 项离线测试 + **现场搜 X 真实跑通**（10 条实时结果 6.1s，带游标）；调度器已自动采集 |
| 4 | 可靠性层（Canary/故障转移/代理） | 🔶 Canary 完成，代理未做（T2） | `canary.py` 三级健康判定 + `/health` 暴露 | 13 项测试 + 真实注入故障验证（能报出连续失败与落后，整体 ok 正确翻转）。代理轮换未做 |
| 5 | MCP 出口 | ✅ 完成 | `mcp_server.py`（与 api.py 平级，零业务逻辑）、`ToolSpec` 协议无关的工具定义 | 11 项测试（含 3 项 REST/MCP 一致性对照）+ 真实 stdio 客户端跑通六个工具 |
| 6 | 迁 RSS + 公众号 channel | ✅ 完成 | RSS 提取器、公众号 channel（mp 后端 + 冷却状态机）、`/api/v1/wechat/feed` | 27 项离线测试 + **真实凭据端到端跑通**：量子位/机器之心共 34 条入库 |

第 2 步的 SKILL.md 已按其内容逐条走查过（见变更节点），修掉端口写错、公众号缺路由、
自相矛盾三处硬伤。仍未在真实 Agent 里端到端跑过，但纸面上的路由与输出规则已验证可用。

### 已上线

- `GET /api/v1/hotlist` — 多平台热榜（缓存），单平台失败不拖垮全局
- `GET /api/v1/items` — 归一化信息流，喂 AIRADAR，带 `since` 增量 + cursor 分页
- `GET /api/v1/health` — 分源采集状态（Canary 做起来之前唯一的可观测窗口）
- `GET /api/v1/article` — 读单篇正文转 Markdown（现查，带 SSRF 防护）
- `GET /api/v1/x/search` — 现场搜 X（**唯一的现查工具**，带超时降级回缓存）
- `GET /api/v1/x/timeline` — 指定用户时间线（优先零认证镜像，省账号配额）
- `GET /api/v1/wechat/feed` — 订阅公众号最新文章（缓存，需自行配置凭据）

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

### 契约里定义的工具

六个全部实现。`search_x` 需要 X 账号 cookie 才能真正工作（见下方已知问题）。

**刻意不给占位端点**——没接的能力就是访问不到，不返回假数据。
SKILL.md 里也写明让 Agent 如实说「这个信源还没接」。

---

## 待办

按「拦路程度」排，不是按工作量。**P0 = 会导致错误行为或安全风险，P1 = 会随时间恶化，
P2 = 体验问题**。

| # | 优先级 | 事项 | 为什么 |
|---|---|---|---|
| T1 | **P0** | 签名密钥过期后自动重取 | `GraphQLBackend` 只在首次用到时解析一次密钥，之后**永不重取**。X 一发版密钥就失效，搜索会一直 404 直到重启进程。模块文档里写了「届时 refresh() 重来」，但那个方法根本没实现——文档承诺了代码没有的东西 |
| T2 | **P0** | 冷却状态持久化 | 冷却只在进程内。真被封号时重启一次就又去捅了——这是账号安全问题，不是体验问题 |
| T3 | **P0** | 声明式引擎识别「业务错误码」 | 很多站点用 HTTP 200 + 响应体错误码表示拒绝（B站 `code`、公众平台 `base_resp.ret`）。引擎只看 HTTP 状态，一律报「多半是对方改版了」。实测 B站已误报过一次（复测 5/5 正常）。**这会让刚做好的 Canary 失去价值**——它分不清「结构变了要改配置」和「临时挡一下退避即可」 |
| T4 | P1 | 跨源去重 | 契约 §2 承诺「按 url 规范化 + 标题相似度归并」，目前**只做了 url 规范化，归并没写**。同一条新闻会以多个源的身份重复进 feed，AIRADAR 那边要自己去重——那是我们答应做的事 |
| T5 | P1 | 数据清理策略 | `items` 表只增不删。这是常驻服务，跑几个月必然膨胀；OpenAI 一家已经 1050 条 |
| T6 | P1 | 推模式（webhook / 队列） | 开发文档 §7 定的是「拉 + 推」两种模式喂 AIRADAR，**推完全没做**。现在 AIRADAR 只能轮询，拿不到「有新增了」的即时信号 |
| T7 | P2 | 并发抓取 | 24 源串行，冷启动约 40s。源越多越难看 |
| T8 | P2 | 代理轮换（接 Clash） | 第 4 步剩下的唯一子项。三级优先级：per-source > 全局 > 环境变量。**现在不急**——只有 X 有 IP 层风险且请求量很低。真做时的工作量不在 `httpx.Client(proxy=)` 那一行，而在配置层级解析、代理自身健康检查、以及和冷却状态机的配合（代理被封该冷却代理，不是冷却账号） |

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
| **搜狗兜不住，已降为可选** | 实测各取 10 条：量子位只出 2 条(含 2019 年的)、机器之心 9 条全是 2017 年、新智元第三个号就撞验证码；`sortType=1&tsn=1` 按时间排序返回 0 条。根因是它给的是按相关性排的搜索结果而非时间流，且每条要额外请求还原跳转 | 默认 `backends: [mp]`。代码保留但不默认启用——静默返回 2017 年文章的兜底比没有兜底更危险 |
| 搜狗给的是限时链接 | 还原出的 `mp.weixin.qq.com/s?src=11&timestamp=…&signature=…` 几小时到一天后失效 | 条目 `raw.link_expires=true` 标注。这是降级路线的固有代价，主力（公众平台）给的是永久链接 |
| **免登录搜 X 已无路可走** | 实测 2026-07-26：Nitter 各实例搜索一律返回 0 条（搜索最费上游配额，是各实例最先关的功能）；xcancel 要 RSS 客户端白名单；X guest token 还能激活但旧的 `/2/search/adaptive.json` 已下线 | `search_x` 只能走登录态 GraphQL。时间线不受影响——Nitter 的时间线实测可用（19 条真推文） |
| X operation id 会随前端发版轮换 | GraphQL 的 queryId 过期表现为 404 | 抽在 `channels/x/config.py` 的 `OPERATIONS` 里，改版=改配置。404 的报错信息直接提示去改那个文件 |
| **签名密钥必须从登录态页面解析** | 匿名访问 x.com 拿到的是 `entry-client-logged-out-*.js` 入口，那个 bundle 里**没有签名脚本**（实测：匿名 35KB/1 chunk，带 cookie 271KB/3 chunk）。twscrape 源码里也有同样的判断，直接抛「Logged-out X web app」 | 签名器改成必须传 cookie，拿到匿名版页面时立刻报清楚原因 |
| ✅ **搜索已完整跑通** | 配上 cookie 后，`search_x` 现场搜 X 返回 10 条实时结果、6.1s、带分页游标；结果自动入库，`live=false` 读缓存 0ms | 落地顺序第 3 步完成 |
| ✅ **签名已实现并端到端验证** | 用自己实现的生成器产出签名，打真实 `SearchTimeline` 拿到 **200 / 133KB / 20 条推文**；同一端点不带签名是 404。anim_key 与独立写的 JS 实现在两组真实输入上逐字符一致 | 见 `channels/x/signature.py`。剩下的只差把 `auth_token` 填进 `config/x_accounts.yaml` |
| verification key 每次请求都变 | 同一页面连续两次抓取拿到完全不同的 48 字节 | 取 key、算 anim_key、发请求必须在一次会话里连贯完成，不能跨请求缓存 key |
| 老版 webpack 构建要重建 chunk 地址 | 登录态页面用的是 responsive-web 老构建，签名脚本 `ondemand.s` 不在 HTML 里，要从页面内两张映射表（683 条哈希 + 616 条名称）拼出地址 | 已实现重建分支。**踩过的坑**：chunk 正则若把 responsive-web 也算进去，会误判成新版而跳过重建，整条路就断了 |
| **搜索强制签名，且签名一次性** | 在真实登录态浏览器里对照验证（2026-07-26）：`UserByScreenName`/`UserTweets`/`UserMedia` 不带签名一律 200；`SearchTimeline` 不带签名 404，**带浏览器刚生成的签名重放依然 404**。最后一条说明签名带时间戳或 nonce，截获不能复用 | 时间线立刻可用；搜索绕不开复刻 twscrape 的 xclid 算法。代码里 `SIGNED_OPERATIONS` 记着这个分化，缺签名器时直接报清楚原因而不是发出去等 404 |
| operation id 与 features 曾经全部过期 | 我凭记忆写的三个 operation id 实测全错，features 也差十几项 | 已用浏览器抓的真实请求校正。这次改动全部集中在 config.py，逻辑一行没动——印证了「常量抽文件」的价值 |
| 文章列表要用 list_ex 不是 appmsgpublish | 参考项目 we-mp-rss 用的是 `appmsgpublish`，返回转义两层的 publish_page（publish_list → publish_info → appmsgex），解析链长且脆；实测同一个号 `appmsg?action=list_ex` 直接给扁平的 app_msg_list，一次 20 条、字段齐全 | 已改用 list_ex，并加测试钉住端点选择 |
| 公众号必须有登录态 | `mp.weixin.qq.com` 的 searchbiz / appmsgpublish 匿名请求一律回 `{"ret":200003,"err_msg":"invalid session"}`（实测 2026-07-26）；微信读书那条路的公众号端点也需登录 | **已用真实凭据跑通**（2026-07-26）：量子位 + 机器之心共 34 条入库，标题/摘要/发布时间/封面图齐全。搜狗那条兜不住已降为可选（见下条）。凭据两条路：浏览器里登录后手动复制 token+cookie（推荐，无自动化痕迹），或跑扫码助手。实测裸 HTTP 的扫码流程可用（startlogin 回 uuid、getqrcode 回真实 JPEG），**不需要 Playwright**——参考项目 we-mp-rss 上浏览器是为了多账号切换和指纹伪装。凭据存在 gitignore 的文件里。**这条线的真实采集从未验证过** |
| 公众号是最易被封的一条线 | 走的是公众平台后台接口，不是官方开放 API | 整块隔离在 `channels/wechat.py`，坏了整块换。账号之间留 3 秒间隔，凭据失效立刻停手不继续捅 |
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
