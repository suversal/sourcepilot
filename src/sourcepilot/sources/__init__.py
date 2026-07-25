"""信源适配层：声明式为主（热榜），重逻辑单写（X、公众号，待建）。"""

from .config import SourceConfig, load_source, load_sources
from .engine import collect, fetch_raw, normalize, rank_to_score

__all__ = [
    "SourceConfig",
    "collect",
    "fetch_raw",
    "load_source",
    "load_sources",
    "normalize",
    "rank_to_score",
]
