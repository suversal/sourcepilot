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

REST 出口 + 热榜链路已跑通，X 后端未开始。

- [x] 工具契约：Item schema、响应信封、错误码、六个工具入参 → [docs/contract.md](docs/contract.md)
- [x] 声明式源引擎 + hotlist + REST + [SKILL.md](SKILL.md)
- [ ] X 后端：签名 + 账号池 + 限流状态机
- [ ] 可靠性层：Canary 自检、故障转移、代理轮换
- [ ] MCP 出口
- [ ] 迁入 RSS + 公众号 channel

已上线端点：`GET /api/v1/hotlist`、`GET /api/v1/items`、`GET /api/v1/health`。
契约里的 `search_x` / `get_x_timeline` / `get_wechat_feed` / `read_article` 尚未实现，
**没有占位端点**——没接的能力就是访问不到，不给假数据。

已接信源：B站排行榜、今日头条热榜、V2EX 最热、掘金后端热榜。
微博配置在仓库里但默认禁用（需要匿名 cookie，见 [config/sources/weibo.yaml](config/sources/weibo.yaml)）。

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

写个 YAML 丢进 `config/sources/`，不用改代码：

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
