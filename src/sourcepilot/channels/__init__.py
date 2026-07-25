"""重逻辑 channel：声明式配置搞不定的信源。

需要登录态、签名或账号池的源单独写 Python，但仍走同一套调度、状态记录与
降级路径——出口层看到的是同一种 Outcome，不因为后端不同而分叉。
"""

from ..sources.engine import register_channel
from .wechat import collect_wechat

register_channel("wechat", collect_wechat)

__all__ = ["collect_wechat"]
