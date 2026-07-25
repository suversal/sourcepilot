"""重逻辑 channel：声明式配置搞不定的信源。

需要登录态、签名或账号池的源单独写 Python，但仍走同一套调度、状态记录与
降级路径——出口层看到的是同一种 Outcome，不因为后端不同而分叉。
"""

from . import wechat, x  # noqa: F401  导入即注册

__all__ = ["wechat", "x"]
