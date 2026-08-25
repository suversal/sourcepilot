# SourcePilot · 进度与待办

> 进度的**唯一真相源**。README 和 CLAUDE.md 只放一句话状态 + 指到这里，
> 避免三处各说各的。
>
> 计划本身（为什么这么拆、每步要解决什么）在 [采集平台开发文档.md](采集平台开发文档.md) §8；
> 契约决议在 [contract.md](contract.md) §0；每次变更的具体理由在 git commit message 里。
> 本文件只回答三个问题：**做到哪了 · 怎么验证的 · 还欠什么**。

最后更新：2026-08-25

---

## 一句话状态

契约 v1.9.0。**6 步落地顺序全部走完**，可靠性层剩代理轮换（T8）。
35 个启用源 / 11 个 REST 端点 / 四个出口（REST · MCP · SKILL.md · RSS）/
持久库 33488 条 / 567 项测试。

**当前红灯有两条**：公众号线被微信读书的人机验证挡着，08-08 起无新增；`pcbeta`
连续返回无法解析的 RSS。其余源继续按各自周期采集，X 实时话题已于 08-25 恢复。

---

## 阶段进度

| # | 阶段 | 状态 | 产出 | 验收依据 |
|---|---|---|---|---|
| 1 | 冻结工具契约 | ✅ 完成 | [contract.md](contract.md) v1.9.0、`src/sourcepilot/contracts/` | 26 项契约不变量测试（`tests/test_contracts.py`） |
| 2 | REST + SKILL.md + 信源接入 | ✅ 完成 | 声明式引擎（JSON/HTML/RSS）、24 个信源、7 个端点、后台调度器、[SKILL.md](../SKILL.md） | 298 项离线测试 + 真实 curl 验证；SKILL.md 已逐条走查并修掉端口/缺路由/自相矛盾三处硬伤 |
| 3 | X 后端（签名/账号池/限流） | ✅ 完成 | 三后端路由、账号池 + 限流状态机、`x-client-transaction-id` 签名、`search_x` 现查降级链、两个 REST 端点 | 39 项离线测试 + **现场搜 X 真实跑通**（10 条实时结果 6.1s，带游标）；调度器已自动采集 |
| 4 | 可靠性层（Canary/故障转移/代理） | 🔶 Canary + 冷却持久化 + 中断告警完成，代理未做（T8） | `canary.py` 三级健康判定 + `/health` 暴露 | 13 项测试 + 真实注入故障验证（能报出连续失败与落后，整体 ok 正确翻转）。代理轮换未做 |
| 5 | MCP 出口 | ✅ 完成 | `mcp_server.py`（与 api.py 平级，零业务逻辑）、`ToolSpec` 协议无关的工具定义 | 11 项测试（含 3 项 REST/MCP 一致性对照）+ 真实 stdio 客户端跑通六个工具 |
| 6 | 迁 RSS + 公众号 channel | ✅ 完成 | RSS 提取器、公众号 channel（mp 后端 + 冷却状态机）、`/api/v1/wechat/feed` | 27 项离线测试 + **真实凭据端到端跑通**：量子位/机器之心共 34 条入库 |

第 2 步的 SKILL.md 已按其内容逐条走查过（见变更节点），修掉端口写错、公众号缺路由、
自相矛盾三处硬伤。仍未在真实 Agent 里端到端跑过，但纸面上的路由与输出规则已验证可用。

### 已上线

- `GET /api/v1/hotlist` — 多平台热榜（缓存），单平台失败不拖垮全局
- `GET /api/v1/items` — 归一化信息流，喂 AIRADAR，带 `since` 增量 + cursor 分页
- `GET /api/v1/feed.xml` — RSS 2.0 订阅源（第四个出口，只出摘要不内联正文）
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
| ~~T1~~ | ✅ 已完成 | ~~签名密钥过期后自动重取~~ | `GraphQLBackend` 只在首次用到时解析一次密钥，之后**永不重取**。X 一发版密钥就失效，搜索会一直 404 直到重启进程。模块文档里写了「届时 refresh() 重来」，但那个方法根本没实现——文档承诺了代码没有的东西 |
| ~~T2~~ | ✅ 已完成 | ~~冷却状态持久化~~ | 冷却只在进程内。真被封号时重启一次就又去捅了——这是账号安全问题，不是体验问题 |
| ~~T3~~ | ✅ 已完成 | ~~声明式引擎识别「业务错误码」~~ | 很多站点用 HTTP 200 + 响应体错误码表示拒绝（B站 `code`、公众平台 `base_resp.ret`）。引擎只看 HTTP 状态，一律报「多半是对方改版了」。实测 B站已误报过一次（复测 5/5 正常）。**这会让刚做好的 Canary 失去价值**——它分不清「结构变了要改配置」和「临时挡一下退避即可」 |
| ~~T4~~ | ❌ 不做 | ~~跨源去重~~ | **已决定不做，并据此改了契约**。归并会抹掉「一条新闻上了 8 个源」这个热度信号，而那正是下游判断热点的依据；采集侧一旦合并，下游再也补不回来。判断「这两条算不算同一件事」需要语义相似度，本质是分析不是采集——和 `categories` 不做 LLM 分类同理 |
| ~~T5~~ | ✅ 已完成 | ~~数据清理策略~~ | `items` 表只增不删。这是常驻服务，跑几个月必然膨胀；OpenAI 一家已经 1050 条 |
| T6 | P2 | 推模式（webhook / 队列） | 开发文档 §7 定的是「拉 + 推」两种模式。**已降级为 P2**：实测增量拉取 `since=` 只要 4ms，AIRADAR 轮询成本几乎为零，且天然幂等容错（挂了重启带上次 since 继续，一条不丢）。推模式只换来几十秒延迟，代价是重试/去重/验签/新端点，还得替消费方记住「哪些推过了」——那等于把它的状态搬到我们这边 |
| T7 | P2 | 并发抓取 | 24 源串行，冷启动约 40s。源越多越难看 |
| T8 | P2 | 代理轮换（接 Clash） | 第 4 步剩下的唯一子项。三级优先级：per-source > 全局 > 环境变量。**现在不急**——只有 X 有 IP 层风险且请求量很低。真做时的工作量不在 `httpx.Client(proxy=)` 那一行，而在配置层级解析、代理自身健康检查、以及和冷却状态机的配合（代理被封该冷却代理，不是冷却账号） |
| ~~T10~~ | ❌ 不做 | ~~补官网源替代公众号覆盖~~ | **2026-08-06 决定不做**。公众号线停用后曾提议用厂商官网源顶上（已探明商汤 `sensetime.com/cn/news`、智源 `hub.baai.ac.cn`、MiniMax、昆仑、零一万物、机器之心静态可抓，其余多为 SPA）。**决策：公众号信源不用官网替代**——官网发的是版本公告与公关稿，公众号发的是解读与一手动态，两者不是同一批内容，用前者冒充后者会让下游以为「这家厂商的动静我们都收着」，而实际漏掉的正是有信息量的那部分。公众号这条线保持停用，等能力恢复或找到真正的公众号路线再说 |
| ~~T11~~ | ✅ 已处理（停用） | ~~修 `qwen` 源~~ | 与上一条**无关**（这是既有 vendor 源坏了，不是公众号替代）：`qwenlm.github.io/blog/index.xml` 还能取到 44 条但最新停在 2025-09，Qwen 已搬到 `qwen.ai`，新站是 SPA、`/rss.xml` 返回同一份 HTML，静态抓不到。要么找它的前端 API，要么这个源摘掉别挂着装样子。**2026-08-18 决定先停用**：
复测确认源还能抓 44 条、格式完整，但最新一篇是 2025-09-22。**抓得到旧内容比抓不到更危险**——
采集成功、Canary 全绿、下游却会以为「Qwen 最近没发布」。理由与复开条件写在 [qwen.yaml](../config/sources/qwen.yaml) 文件头。
真要恢复得找 qwen.ai 的列表 API，属重逻辑而非改配置 |
| ~~T12~~ | ✅ 已完成 | ~~采集中断告警~~ | **这次事故真正的教训**：公众号线 08-08 就停了，直到 08-17 才被发现——中间 9 天，`/health` 一直能查出来，但没人去查。Canary 已有三级健康判定，缺的是「连续 N 轮失败就主动吼一声」的出口（日志之外的推送/邮件/webhook 任选其一）。**这条比修任何一个源都重要**：一个源坏掉是必然的，9 天发现不了才是问题。
**2026-08-18 完成**：`alert.py` 走 Telegram（与 AIRADAR 的 `telegram_notifier` 同一对环境变量，
同一个机器人直接复用）。只在状态转换时推（`degraded` 不推，太吵）；已推送状态存 `alert_state` 表
（重启不重推）；推送失败不更新状态、下轮重试（先记后发会永久吞掉告警）；best-effort，
绝不阻塞采集。真实通道已验证，生产库 dry-run 正确推出 bilibili + wechat 两条 |
| T13 | P1 | weread 撞验证码后的降级行为 | 现在撞了 CAPTCHA 就整条线停在那，库里的旧文照常提供（这部分是对的），但**没有任何对外信号**说「这个源的数据不新了」。契约有 `meta.stale`，`/wechat/feed` 该在后端处于验证码冷却期时把它置上，让下游知道手里是旧数据 |
| ~~T14~~ | ✅ 已完成 | ~~X 实时话题签名恢复与冷却隔离~~ | 2026-08-25 X 的 responsive-web chunk hash 从 7 位切到 16 位，旧正则找不到 `ondemand.s`，却误报 `AUTH_EXPIRED` 并把整个 GraphQL 后端冷却 6 小时。现已兼容 7/16 位 hash；页面结构故障改报 `UPSTREAM_DOWN`；搜索冷却键拆为 `x:graphql:SearchTimeline`，不再拖累无需签名的 `UserTweets`。真实 `SearchTimeline` 验证 `HTTP 200 / mode=live / stale=false / 20 条`，567 项测试通过 |
| T9 | P2 | `/items` 按公众号过滤（或 `/wechat/feed` 加 `since`） | AIRADAR Phase 1 接入实测发现（2026-08-04）：`/items` 的 `platform` 白名单只认信源配置名（`wechat` 整体算一个），不认具体公众号名；按号过滤只能走 `/wechat/feed`，而它没有 `since` 参数（契约 §4 如此）。下游已用 `/wechat/feed` + 客户端水位过滤落地，本机毫秒级查询下没有实际代价，所以只是 P2。真做时二选一：platform 白名单纳入公众号名，或 `/wechat/feed` 加 `since`（加可选参数属 minor） |

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
| responsive-web 构建要重建 chunk 地址 | 登录态页面的签名脚本 `ondemand.s` 不在 HTML 里，要从页面内哈希表与名称表拼地址。2026-08-25 hash 从 7 位切到 16 位，旧解析器因此从 02:52 起每 6 小时失败一次 | 已同时支持 7/16 位 hash；16 位页面实测识别 1023 个 chunk，构造出的 `ondemand.s` 返回 200 并解析出 4 个索引。搜索结构故障不再误伤时间线 |
| **搜索强制签名，且签名一次性** | 在真实登录态浏览器里对照验证（2026-07-26）：`UserByScreenName`/`UserTweets`/`UserMedia` 不带签名一律 200；`SearchTimeline` 不带签名 404，**带浏览器刚生成的签名重放依然 404**。最后一条说明签名带时间戳或 nonce，截获不能复用 | 时间线立刻可用；搜索绕不开复刻 twscrape 的 xclid 算法。代码里 `SIGNED_OPERATIONS` 记着这个分化，缺签名器时直接报清楚原因而不是发出去等 404 |
| operation id 与 features 曾经全部过期 | 我凭记忆写的三个 operation id 实测全错，features 也差十几项 | 已用浏览器抓的真实请求校正。这次改动全部集中在 config.py，逻辑一行没动——印证了「常量抽文件」的价值 |
| B站会用 HTTP 200 + code=-352 限流 | 实测确认：`HTTP 200 / code=-352 / message="-352" / data=null`。之前这会被报成「多半是对方改版了」 | 已在 bilibili.yaml 声明 `status` 规则，现在正确报 `RATE_LIMITED`，冷却状态机会退避 |
| 文章列表要用 list_ex 不是 appmsgpublish | 参考项目 we-mp-rss 用的是 `appmsgpublish`，返回转义两层的 publish_page（publish_list → publish_info → appmsgex），解析链长且脆；实测同一个号 `appmsg?action=list_ex` 直接给扁平的 app_msg_list，一次 20 条、字段齐全 | 已改用 list_ex，并加测试钉住端点选择 |
| 公众号必须有登录态 | `mp.weixin.qq.com` 的 searchbiz / appmsgpublish 匿名请求一律回 `{"ret":200003,"err_msg":"invalid session"}`（实测 2026-07-26）；微信读书那条路的公众号端点也需登录 | **已用真实凭据跑通**（2026-07-26）：量子位 + 机器之心共 34 条入库，标题/摘要/发布时间/封面图齐全。搜狗那条兜不住已降为可选（见下条）。凭据两条路：浏览器里登录后手动复制 token+cookie（推荐，无自动化痕迹），或跑扫码助手。实测裸 HTTP 的扫码流程可用（startlogin 回 uuid、getqrcode 回真实 JPEG），**不需要 Playwright**——参考项目 we-mp-rss 上浏览器是为了多账号切换和指纹伪装。凭据存在 gitignore 的文件里。**这条线的真实采集从未验证过** |
| 公众号是最易被封的一条线 | 走的是公众平台后台接口，不是官方开放 API | 整块隔离在 `channels/wechat.py`，坏了整块换。账号之间留 3 秒间隔，凭据失效立刻停手不继续捅 |
| ✅ **公众号已靠微信读书恢复（主力换 weread）** | 2026-08-06：微信读书是与公众平台**完全独立的另一套系统**，不受 7-30 那次关闭影响——实测公开的 Wechat2RSS 服务（其部署文档写明「通过读书获取公众号信息」）在关闭之后仍输出当天文章（量子位 08-05 14:08、机器之心 08-05 14:10，均带全文）。关键换算：**微信读书把公众号当「书」，`bookId = "MP_WXS_" + base64解码(fakeid)`**，而 fakeid 就是 `__biz`，我们 23 个号全都有，不必像其它实现那样为每个号手工找一篇文章链接 | 新增 `channels/wechat/weread.py`（+ `weread_check.py` 自检），`backends: [weread]`。**三个坑已写进代码注释**：① `/web/mp/articles` 必须带阅读器页 Referer，否则恒 `-2041`（上下文校验，不是限流），而那串 hash 每个号不同、只能从书架 `deepLink` 取；② 一次群发是一个 `reviews` 条目，`subReviews` 才是一篇篇文章，只读 `[0]` 会漏；③ 有反爬，`min_interval` 设 6 小时（一轮 24 个请求） |
| ❌ ~~跨公众号列表能力被微信关闭，channel 已停用~~（mp 后端仍不可用，channel 已由 weread 顶上） | 2026-08-06 实测：`appmsg?action=list_ex` 恒回 `{"ret":200013,"err_msg":"freq control"}`，而**同一套凭据打 searchbiz 仍 `ret=0`**。四组对照全部排除：换公众号主体、换个人微信号(wxuin)、IPv6/强制 IPv4 出口——均 200013。同期三个开源项目报同一现象且共同特征是**第一页零结果就被拒**（历史累计频控是跑几小时后才出现）：we-mp-rss #440（7-30，已独立核实正文含 `ret==200013` + `stop at 0`）、wechat-article-exporter #199、wechat-download-api #22 | **`enabled: false`**，理由与复开条件写在 [wechat.yaml](../config/sources/wechat.yaml) 文件头。`appmsg` 的 200013 现在报「不是临时频控」而非「稍后重试」——后者会把运维带向换号/等待的死路（我们为此白花了几天）。能力恢复的判据：`python -m sourcepilot.channels.wechat.check` 的 appmsg 回 `ret=0` |
| `/api/v1/article` 保留，但 AIRADAR 不再用它取公众号正文 | 2026-08-06 复核：AIRADAR 自己就有能抓公众号的提取器（`article_content.py` 的 `wechat-article-v1` profile，草稿管理天天在用）。实测同一篇文章两边覆盖度一致（都是 2 个小标题 + 7 张图），绕来 SP 只是多一跳、多一处跟着微信改版维护的代码 | **端点保留**——它是契约里的六个工具之一，MCP 出口与其它消费方仍需要，不因为某个下游不用了就删。AIRADAR 侧已改回自己的 `fetch_manual_article()`，理由与实测数据记在 `HotAI/docs/sourcepilot-integration-plan.md` §9.2 |
| 微信读书回的文章 id 把 `_` 换成了 `~` | 微信文章 id 用 base64url 字符集（`A-Za-z0-9_-`），**不含 `~`**。原样拼进 URL 会得到「参数错误」页。实测 2026-08-06：`XK6ymJL7y0vo~GQXxmpuBA` 打不开，换成 `_` 后正常（DeepSeek-V3 那篇）；换成 `-` 仍是参数错误；`~~` 连着的同理 | `normalize_original_id()` 只替换 `~`，**不动 `-`**——真实 id 里本来就有连字符（如 `nL--rVri3qAy~6Recsg~4g`），一起换会把好 id 改坏。库里已产生的 63 条（SP）+ 13 条（AIRADAR）坏链接已就地修复，AIRADAR 侧连 `url_hash` 一起改，否则下轮会当新文重复入库 |
| **书架不是准入名单，只是一张通行证** | `-2041` 那道校验只认「Referer 是不是合法的阅读器页」，**不比对 bookId**。实测 2026-08-06：拿书架里某个 Kimi 号的阅读器页当 Referer，去拉根本不在书架里的量子位，返回 77 篇、机器之心 53 篇，最新都是当天的 | 所以**要订阅的号不必逐个加进微信读书书架**——参考实现里那条前提是多余的。书架里有任意一个公众号能换到通行证即可，一次性设置。`WereadClient.reader_ticket()` 干这件事 |
| weread 的固有局限（平台侧，别当 bug 修） | **收录滞后**：部分号在微信读书侧收录慢，实测遇到过滞后半个多月的；**不实时**：通常比发布晚几小时；**有反爬**：参考实现作者一天 30 多次快速请求就白屏几小时；**fakeid 是必需的**（微信读书网页端搜不到公众号名，实测多种参数组合全返回图书，没法按名字兜底） | 撞风控就把 `min_interval` 与 `account_interval` 一起往上调，别指望重试。实测一轮 23 个号 89 秒、230 条，零失败 |
| ~~三条替代路线全部探过，当前无自持方案~~（微信读书那条实为可用，见上） | **搜狗**：账号索引已死（`type=1` 回「暂无相关的官方认证订阅号」）；文章搜索还在但是按相关性排的关键词搜索——搜「量子位」9 条里 8 条是 2019–2022 老文，还混进「量子位=qubit」的量子计算文章，**移动入口结果不带发布者字段**，做不了「精确发布者过滤」；`usip`/`tsn`/`sortType` 一律返回空页。**wewe-rss**：自己不实现微信读书协议，转发给作者托管的 `weread.111965.xyz`（无 token 回 401），不能当参考实现。**Wechat2RSS**：现成可用（¥150/年私有部署），但用户协议禁止商用和内容分发。**profile_ext/getmsg**：需 `__biz`/`uin`/`key` 这套客户端短期凭据，与后台 token+cookie 不是同一模型，key 二十分钟级过期 | **注意别拿 `weread.qq.com/web/search/global` 验证微信读书能不能用**——它只搜图书，公众号走的是 `/web/mp/*` 那组需登录态的接口，是两回事。我第一次就是这么误判成「微信读书也断了」的。**qwen 源已失效待修**（qwenlm.github.io 停在 2025-09，新站 qwen.ai 是 SPA 抓不了），见 T11 |
| ✅ **AIRADAR 已接入公众号信源（下游 Phase 1 落地）** | 2026-08-04：AIRADAR 侧新增 `SourcePilotCrawler`，23 个公众号各建一个 `type=sourcepilot` 源，走 `/wechat/feed?account=` 拉取 + `/article` 补全文（htmlmd 提取实测量子位 27 篇 20 篇全文）；完整管线验证 27 抓取/18 入选/19 落库。方案与实施记录在 `HotAI/docs/sourcepilot-integration-plan.md` §8 | 接入中发现的契约缺口记 T9。SP 侧本次零改动 |
| ✅ **X 话题订阅上线（契约 1.9.0，§5.5）** | 2026-08-08：`x.yaml` 新增 `topics` 节做事件追踪——账号订阅盖「官方说了什么」，话题订阅盖「事件下大家在说什么」。定时采集逐话题跑搜索（走登录账号池），结果标 `origin=topic` 进信息流（升级链 collected > topic > searched）；`x_tweets` 加 `topics` 列（合并语义），`/x/tweets` 加 `topic` 过滤。噪音靠三道确定性闸门：话题可按目标选 X 的 Top 热门排序或 Latest 最新排序 + query 里的 `min_faves:` + 采集侧 `min_likes` 兜底。当前三个话题 `AI热点` / `U卡推荐` / `eSIM推荐` 均使用 Latest、`min_faves:50` 与 `min_likes:50`，优先保留新鲜且已有基础互动的数据；账号订阅为 19 个国内外官方账号加 3 个个人账号，共 22 个。2026-08-25 实测完整一轮：22 次 `UserByScreenName`、22 次 `UserTweets`、3 次 `SearchTimeline` 和 10 次长文请求全部 200；`UserTweets` 的 15 分钟配额 50 次、使用后剩 28，未触发 429；账号时间线返回 378 条，数据库新增 265 条推文，`AI热点` 首轮落库 20 条。2026-08-26 将三个持久化标识从英文迁为上述中文名，正式库历史标签全部就地迁移且旧标签归零；随后实测 U卡 Latest+50 返回最近 10 天的 10 条、eSIM Latest+50 返回最近 2 天的 10 条。AIRADAR 已接入逐 topic 拉取与话题筛选，但其镜像清单需另行同步。 | 话题要**少而精**：每话题每轮一次搜索，配额与封号风险同 `search_x`。AR 侧话题清单镜像在 `SOURCEPILOT_X_TOPICS`，账号清单镜像在 `x_tweets_sync.DEFAULT_X_HANDLES` 或环境变量；本轮按用户要求只整理 SP，AIRADAR 尚未迁移中文话题名。 |
| ✅ **AIRADAR 已接入 X 推文（下游 Phase 4 落地）** | 2026-08-08：AIRADAR 侧新增 `x_tweets` 镜像表 + 同步服务，逐订阅 handle 拉 `/api/v1/x/tweets?handle=`，首轮 81 条入库、`/x` 页按 `content_kind` 分流渲染跑通。**接入中验证了契约 §5.4 的一个后果**：`/x/tweets` 不分 collected/searched，别人现查捞回的杂音也在里面（164 条里只有 81 条属订阅账号），消费方必须按 handle 自守内容边界——这是契约设计的预期行为，不是缺口。实施记录在 `HotAI/docs/sourcepilot-integration-plan.md` §8.5 | SP 侧本次零改动。AIRADAR 的订阅 handle 列表镜像自 `config/sources/x.yaml` 的 accounts——**改这份订阅时记得同步改 AIRADAR 侧**（`x_tweets_sync.DEFAULT_X_HANDLES` 或其环境变量），SP 没有暴露订阅清单的端点 |
| 无代理支持 | Clash 三级优先级（per-source > 全局 > 环境变量）未接 | 抓 X 之前必须补上 |
| ⚠️ **出口 IP 被腾讯风控标记，公众号线中断** | 2026-08-17 排查：错误链是**出口 IP 被标记 → 验证码控制接口拒绝（「您的操作过于频繁」）→ `/web/mp/articles` 回 `-2041` → 采集停摆**。把四种可能逐个排除掉才定到 IP 层：换新账号（当天新注册的收集专用号）同样被拒；**真浏览器**（Playwright + 真人扫码登录、完整指纹）同样被拒；`-2014` 是频率限制、`-2041` 是验证失败，两者在同一 IP 下交替出现。共同点是**只要从这个出口出去就被拒**，与账号质量、指纹、自动化痕迹都无关。IP 是日常使用与采集共用的那一条（上海电信家宽），当天一小时内跑了自检 + 完整采集轮 + 页面/封面对照 + 三个浏览器探针，把它烤热了。2026-08-18 复测仍是 `CAPTCHA`（书架接口与阅读器页通行证都正常，24 个号都能定位 bookId，卡在真正拉文章那一步）——腾讯这类冷却通常是几小时到一天，超过一天说明标记还没解 | 所有探测脚本已全部停掉（反复探测正是让它持续被标记的原因）。**代码侧不需要改**，这不是 bug。要恢复有三条路，按代价排：① 换出口（手机热点验证 IP 假设，或接 T8 的代理轮换）；② 等标记自然过期，期间别再探；③ 把采集账号与日常账号的出口彻底分开。**真正要补的是 T12 告警**——这次是 9 天后才发现，而 `/health` 一直答得出来 |
| `read_article` 在 fake-ip 代理下拒绝一切 URL | 2026-08-18 发现：Clash 这类代理在 fake-ip 模式下不做真实 DNS，把每个域名映射到 `198.18.0.0/16`（IPv6 侧 `fdfe:dcba:9876::/48`）的占位地址，而 Python 的 `is_private` 把那个段算作私网 → SSRF 校验把**每一个公网域名**判成内网，`read_article` 整个工具静默失效（而部署方式正是 Mac mini + Clash Verge）。同时它让 4 项 SSRF 测试在开着代理的机器上失败，而代码完全正确 | 已修：这些段按「域名的占位地址」而非「内网地址」处理，`SOURCEPILOT_FAKE_IP_CIDRS` 可配可关。**例外只给域名**——字面量不经 DNS（`http://198.18.0.111/` 照旧拒），内网域名走真实解析拿到私网地址（照旧拒）。测试的 DNS 一并打桩（`tests/test_article.py::stub_dns`），536 项现在真的全离线。端到端复验：抓 openai.com 一篇回 11509 字正文 |

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
| 国产大模型一手信息走公众号而非官网 | 2026-07-26 | 实测 12 家国产厂商官网：只有商汤、昆仑万维、月之暗面是 SSR，其余（百度、腾讯混元、讯飞、MiniMax、百川、零一万物、阶跃、面壁）全是 SPA 且都没有 RSS。而它们的公众号**每周都在更新**。所以覆盖国产厂商靠的不是加 RSS 源，是扩公众号订阅：2 个媒体号 → 19 个号（17 个厂商官方） |
| 公众号账号配 fakeid 而非名字 | 2026-07-26 | 按名字搜会搜错号——实测「智谱AI」命中 2022 年停更的同名号、「Kimi」命中 2018 年讲电影票的无关号。顺带每轮请求数减半（19 而非 38），而搜索是公众平台上最容易触发风控的动作。配 `lookup` 工具查 fakeid，它同时报最近更新日期 |
| 信源以 AIRADAR 数据库的实际启用状态为准 | 2026-07-26 | 第一版照 `data/sources.json` 接的，那是 seed 文件、39 个源全 `is_active=true`；数据库里实际是 23 启用 / 24 禁用。核对后补了 4 个漏的（Cursor、GitHub AI&ML、GitHub 工程、NVIDIA 开发者），关掉 11 个 AIRADAR 已禁用的并清了它们的 257 条数据（`rss` 类型保留期是永久，不清会一直推给下游）。**接配置文件不等于接实际状态** |
| 迁入 AIRADAR 的 RSS 源，信源 24 → 44 | 2026-07-26 | 逐个实测 33 个候选：30 可用，linuxdo 的 RSS 加 `impersonate` 也能过、reddit 的 429 是并发探测打出来的，只有 GitHub Trending 真没有可用 RSS（官方无、rsshub 403）。改 RSS 的判据是字段质量而非「RSS 更正统」——36氪 HTML 版 55 条时间/摘要/作者全无、IT之家 169 条零摘要，RSS 版都有；少数派 JSON 30 条 vs RSS 10 条、HN 的 Algolia 有 points 而 RSS 没有，这两个就保持原样 |
| 采集节流三件套 | 2026-07-26 | 实测量化后按收益排序：**频率是大头**（8 个 vendor 源每天抓 456 次产出 0 条，改 1 小时后降到 192）；条件请求省带宽不省时间（RTT 主导，且 26 源里只有 qwen 支持 ETag）；`max_items` 收益最小（feedparser 得先解析完整个 XML，截断只省最后一步）。三条都做了，但别指望后两条 |
| 现查结果不进信息流（契约 1.5.0） | 2026-07-26 | `search_x` 的现查结果一直在落库兜底降级，但也混进了 `/items` 与 RSS。一次 `q=Opus` 的实测把「Barbie is his magnum opus?」推到了信息流最前面——**任何人的一次临时查询都会污染所有订阅者的内容**。加 `items.origin` 区分 `collected`/`searched`，只有降级链读得到后者 |
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
| （中间提交见 git log） | 字节 Seed 博客流、X 推文全貌 `x_tweets`、长文抓取、转发识别、GraphQL-first 等，契约推进至 1.6.0 |
| `f954e41` | 富文本样式落地（契约 1.7.0）：article 正文补行内加粗/斜体；note 长推的 `richtext_tags` 入库并织进 `display_text`；库存 3 篇 article 已重取带样式 |
| （近期提交） | 配图拼进 `display_text`（契约 1.8.0）：图片 `![](url)` 织入/追加，视频给可点击缩略图，正文里指向媒体的 t.co 残链清掉；article 不重复拼（配图已内嵌） |
| `b56a328` | 发布前整理：修 `read_article` 在 fake-ip 代理下全拒（并把 SSRF 测试的 DNS 打桩，536 项真离线）；补 `trafilatura` 硬依赖与 `mcp` 可选依赖声明；`.gitignore` 补上漏掉的 `weread_collector.yaml`（含真实 cookie）与 `.claude/`；停用失效的 `qwen` 源；补 weread / 公众平台凭据模板；README 与本文件的数字、信源清单、公众号名单全部按库里实测值刷新 |
| `64ae651` | 采集中断告警（T12）：Canary 判定发生转换时推 Telegram，复用 AIRADAR 那个机器人。9 天没发现故障的直接对策 |
| `9164815` | 配置落地：`settings.py` 自读项目根 `.env`（真实环境变量优先），让 IDEA / 命令行 / cron 三种起法共用一份配置——凭据不能写进 `.idea/runConfigurations/*.xml`，那些文件跟着仓库走。补 `.env.example`；启动日志报告告警是否启用；LICENSE 署名改 suversal |
| `511fed1` | MCP 出口兼容 mcp 2.x：**CI 第一次跑就抓到的问题**——2.0 把低层 Server 的装饰器（`@server.list_tools()`）换成构造参数回调（`on_list_tools=`），本机装的 1.28.1 全绿、干净环境装到 2.0.0 整个出口 `AttributeError`。按能力探测分流两套 API（不读版本号），并补上 `create_server` 的测试覆盖——原先所有 MCP 测试都只覆盖协议无关的那半，构造服务器这一步没人碰。CI 的 mcp 档改成 1.x / 2.x 双 matrix，在两个真实 venv 里各跑通全套 564 项 |
| `d23b029` | 修 X responsive-web 16 位 chunk hash；签名结构故障与账号失效分流；SearchTimeline 冷却不再拖累 UserTweets；合并 Claude 临时运行库与仓库主库并切换到持久库；订阅扩为 19 个官方账号与 `ai-hot` / `u-card` / `esim` 三个话题；账号整轮与真实话题搜索通过，567 项离线测试通过 |
| `1ee10b7` | X 订阅补回 `thsottiaux`、`xiaohu`、`dotey` 三个个人账号，总数增至 22；正式库完整同步一轮，57 次 GraphQL 请求全部 200、0 次 429，新增 265 条推文；服务重启到 AR 使用的 `127.0.0.1:8420` 并加载新配置 |
| `6142bf0` | 三个话题持久化标识完整迁移为 `AI 热点` / `U卡推荐` / `eSIM推荐`；正式库迁移前已备份，历史标签分别保留 20 / 253 / 274 条，旧英文标签归零；补中文标识契约说明与配置回归测试 |
| `0621324` | `AI 热点` 去掉中间空格，配置与正式库 20 条历史标签统一迁为 `AI热点`；`U卡推荐` / `eSIM推荐` 不变 |
| 本次（待提交） | 三个话题统一使用 Latest、`min_faves:50`、`min_likes:50` 并过滤回复/转推；收紧 AI 品牌词及 U卡/eSIM 实测类关键词，减少歧义和旧数据占位 |
