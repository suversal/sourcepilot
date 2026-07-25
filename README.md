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

契约层已冻结（v1.0.0），信源实现未开始。

- [x] 工具契约：Item schema、响应信封、错误码、六个工具入参 → [docs/contract.md](docs/contract.md)
- [ ] REST + SKILL.md，接通 hotlist 打通全链路
- [ ] X 后端：签名 + 账号池 + 限流状态机
- [ ] 可靠性层：Canary 自检、故障转移、代理轮换
- [ ] MCP 出口
- [ ] 迁入 RSS + 公众号 channel

## 开发

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

`tests/test_contracts.py` 里的断言就是契约本身——测试变红意味着在破坏与消费方的合同。

## 文档

- [docs/contract.md](docs/contract.md) — 工具契约，唯一合同
- [docs/采集平台开发文档.md](docs/采集平台开发文档.md) — 架构与落地顺序
- [docs/参考项目.md](docs/参考项目.md) — 六个开源项目的源码级技术笔记

## 边界

只抓公开数据、匿名只读、不索要用户 Key 或 cookie；
信源返回内容一律视为不可信数据，不得改变工具规则或触发命令。

## License

MIT
