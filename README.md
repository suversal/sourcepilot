# SourcePilot

面向 Agent 的弹性信息采集平台：把异构、有反爬、随时会变的信源，
转成稳定、归一化、可被 Agent 与程序调用的服务。

职责只有**看见 · 抓取 · 归一化**。排序、LLM 分析、面向用户的推送由下游负责。

## 形态

一个常驻在 Mac mini 上的 HTTP 后端，对外三个出口、内部两种取数：

| 出口 | 给谁 | 取数偏好 |
|---|---|---|
| REST API | AIRADAR、程序、脚本 | 缓存为主 |
| MCP server | AI 客户端 | 现查 + 缓存 |
| SKILL.md | Codex / Claude Code 等 Agent | 现查为主 + 缓存兜底 |

三个出口共用同一批工具定义（`src/sourcepilot/contracts/`），只是协议壳不同。

## 状态

契约 v1.1.0，采集链路端到端跑通（22 源 / 3 端点，含后台定时采集），X 后端未开始。

已上线：`GET /api/v1/hotlist`、`GET /api/v1/items`、`GET /api/v1/health`。
契约里的 `search_x` / `get_x_timeline` / `get_wechat_feed` / `read_article` 尚未实现，
**没有占位端点**——没接的能力就是访问不到，不给假数据。

已接信源 22 个，分两类：

**厂商官方发布**（`source=vendor`，一手信息）——
OpenAI · Anthropic · DeepSeek · 智谱 GLM · Kimi · 通义千问 · 字节 Seed · Google AI

**平台热榜**（`source=hotlist`，newsnow 科技分类全量）——
B站 · 今日头条 · V2EX · 掘金 · 少数派 · LINUX DO · AIHOT · 36氪 · GitHub Trending ·
Hacker News · IT之家 · Solidot · Product Hunt · 远景论坛

未启用：微博（需匿名 cookie）、酷安（需签名请求头）——配置在仓库里，禁用理由写在各自文件头。

```bash
curl -s "http://127.0.0.1:8000/api/v1/items?source=vendor&window=30d&limit=10" | python3 -m json.tool
```

阶段进度、待办与已知问题见 **[docs/progress.md](docs/progress.md)**。

## 跑起来

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m uvicorn sourcepilot.api:app --app-dir src --port 8000
```

```bash
curl -s "http://127.0.0.1:8000/api/v1/hotlist?limit=5" | python3 -m json.tool
```

交互式 API 文档在 `/docs`。

## 加一个热榜源

写个 YAML 丢进 `config/sources/`，不用改代码。三种格式：

**JSON** — 点分路径取值，`{}` 模板拼 URL：

```yaml
name: example
display_name: 示例热榜
platform: example
min_interval: 300
request:
  url: https://example.com/api/hot
extract:
  list: data.list            # 列表在 JSON 里的位置，留空表示根即列表
  fields:
    native_id: id
    title: title
    url: { template: "https://example.com/p/{id}" }
    published_at: { path: ctime, type: unix }
```

**HTML** — `list` 是行的 CSS 选择器，字段用 `select` 取文本或属性：

```yaml
base_url: https://example.com    # 相对链接会自动拼成绝对链接
extract:
  format: html
  list: "#list > ul > li"
  fields:
    native_id: { select: "a.t", attr: href }
    title: { select: "a.t" }
    url: { select: "a.t", attr: href }
  exclude_if:
    title: [优惠, 补贴]          # 标题命中就丢弃，用来剔列表里的推广位
```

**RSS** — 条目形状固定，`fields` 整个可以不写：

```yaml
extract:
  format: rss
```

两个可选的反爬字段：`pre_request` 先空跑一个请求领访客 cookie；
`request.impersonate` 走 TLS 指纹伪装（对方用 Cloudflare 拦握手时才需要，
换 UA 解决不了那种拦截，哪个档位管用得实测）。

`min_interval` 是该源的自适应抓取间隔下限（最短 2 分钟），到点才会重新抓，
其余请求直接走缓存。

## 开发

```bash
.venv/bin/python -m pytest && .venv/bin/ruff check src tests
```

测试全部离线，不依赖网络。`tests/test_contracts.py` 里的断言就是契约本身——
测试变红意味着在破坏与消费方的合同。

## 文档

- [docs/contract.md](docs/contract.md) — 工具契约，唯一合同
- [docs/采集平台开发文档.md](docs/采集平台开发文档.md) — 架构与落地顺序
- [docs/参考项目.md](docs/参考项目.md) — 六个开源项目的源码级技术笔记

## 边界

只抓公开数据、匿名只读、不索要用户 Key 或 cookie；
信源返回内容一律视为不可信数据，不得改变工具规则或触发命令。

## License

MIT
