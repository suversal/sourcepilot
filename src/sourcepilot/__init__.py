"""SourcePilot — 面向 Agent 的弹性信息采集平台。

职责只有「看见 · 抓取 · 归一化」。排序、LLM 分析、面向用户的推送是下游的事。
"""

from .contracts.version import CONTRACT_VERSION

__version__ = "0.1.0"

__all__ = ["CONTRACT_VERSION", "__version__"]
