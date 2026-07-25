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

契约已冻结，热榜链路端到端跑通（14 源 / 3 端点，含 newsnow 科技分类全量），X 后端未动。
6 步落地顺序完成前 2 步。

---

## 阶段进度

| # | 阶段 | 状态 | 产出 | 验收依据 |
|---|---|---|---|---|
| 1 | 冻结工具契约 | ✅ 完成 | [contract.md](contract.md) v1.0.0、`src/sourcepilot/contracts/` | 26 项契约不变量测试（`tests/test_contracts.py`） |
| 2 | REST + SKILL.md + 首个信源 | ⚠️ 基本完成 | 声明式引擎（JSON/HTML/RSS）、14 个热榜源、3 个端点、[SKILL.md](../SKILL.md) | 109 项离线测试 + 真实 uvicorn curl 验证 |
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

已接信源 14 个，覆盖 newsnow 科技分类全量：

| 格式 | 信源 |
|---|---|
| JSON | B站排行榜 · 今日头条 · V2EX · 掘金 · 少数派 · LINUX DO · AIHOT |
| HTML | 36氪快讯 · GitHub Trending · Hacker News · IT之家 |
| RSS | Solidot · Product Hunt · 远景论坛 |

引擎为此扩了三项能力：HTML/CSS 选择器提取、RSS/Atom 提取（顺带把第 6 步的
RSS 能力提前做了）、`request.impersonate` TLS 指纹伪装。

性能：冷启动约 10.8s（14 源串行抓），间隔内约 29ms（走缓存）。

### 契约里定义但尚未实现

`search_x` · `get_x_timeline` · `get_wechat_feed` · `read_article`

**刻意不给占位端点**——没接的能力就是访问不到，不返回假数据。
SKILL.md 里也写明让 Agent 如实说「这个信源还没接」。

---

## 待办

按「拦路程度」排，不是按工作量。

| # | 事项 | 为什么重要 |
|---|---|---|
| T1 | 用 Codex 装 SKILL.md 实测整链路 | 现在便宜，等 X 写完再回头改 SKILL.md 要贵得多 |
| T2 | 定时调度器 | 当前刷新是「请求到来时顺带刷」，**没人访问就永不更新**。AIRADAR 靠 `/items` 拉增量，冷启动时库是空的 |
| T3 | X 后端签名可行性验证 | 第 3 步的全部价值都押在这上面。动手写之前先确认 `x-client-transaction-id` 那套现在还有效 |
| T4 | 跨源去重 | 契约说按 url 规范化 + 标题相似度归并。目前**只做了 url 规范化，归并逻辑没写**，跨源重复条目会直接进 feed |
| T5 | 并发抓取 | 14 源串行，冷启动 10.8s。源多了这个数字只会更难看 |
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
| 分类规则表很稀 | `source_rules` 为空，关键词表只有 v1 词条 | `categories` 命中率低，下游只能当过滤辅助用 |
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
| 接入 newsnow 科技分类全量 | 2026-07-25 | 为此给引擎补了 HTML 与 RSS 提取器——「新增源=改配置」只有在引擎覆盖信源实际用的格式时才成立 |
| 反爬手段做成配置而非代码 | 2026-07-25 | `impersonate`、`pre_request`、`exclude_if` 都是配置字段。对方改策略时改一行 YAML，不动逻辑 |

---

## 变更节点

| 提交 | 内容 |
|---|---|
| `9bc8ba5` | 契约层 v1.0.0：冻结 Item / 信封 / 错误码 / 六工具入参 |
| `2f945ba` | 声明式热榜引擎 + REST 出口 + SKILL.md |
| `63e5c54` | 补进度文档，收拢三处漂移的状态记录 |
| 本次 | 接入 newsnow 科技分类全量：引擎补 HTML/RSS 提取器 + TLS 指纹伪装，信源 4 → 14 |
