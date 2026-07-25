from __future__ import annotations

import pytest

from sourcepilot.sources import SourceConfig
from sourcepilot.store import Store

#: 一份仿真载荷，覆盖引擎要处理的几种形状：嵌套列表、深层字段、模板拼 URL、
#: unix 时间戳、以及一条缺必填字段的脏数据。
FAKE_PAYLOAD = {
    "code": 0,
    "data": {
        "list": [
            {
                "vid": "AAA111",
                "title": "某大模型发布新版本",
                "desc": "官方摘要",
                "owner": {"name": "作者甲"},
                "pubdate": 1784711789,
                "stat": {"view": 5000},
                "pic": "https://example.com/a.jpg",
            },
            {
                "vid": "BBB222",
                "title": "一篇教程",
                "owner": {"name": "作者乙"},
                "stat": {"view": 100},
            },
            {"title": "缺 native_id 的脏数据"},
        ]
    },
}

FAKE_CONFIG_DICT = {
    "name": "fake",
    "display_name": "测试源",
    "type": "hotlist",
    "platform": "fake",
    "min_interval": 300,
    "ranked": True,
    "lang": "zh",
    "request": {"url": "https://example.com/api"},
    "extract": {
        "list": "data.list",
        "fields": {
            "native_id": "vid",
            "title": "title",
            "url": {"template": "https://example.com/v/{vid}"},
            "summary": "desc",
            "author": "owner.name",
            "published_at": {"path": "pubdate", "type": "unix"},
            "score_raw": {"path": "stat.view", "type": "int"},
            "image": "pic",
        },
    },
}


@pytest.fixture
def fake_config() -> SourceConfig:
    return SourceConfig(**FAKE_CONFIG_DICT)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")
