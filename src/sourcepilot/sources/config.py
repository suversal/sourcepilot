"""声明式信源配置。

新增一个热榜源 = 加一个 YAML 文件，不改代码。抓取技巧（伪造 UA、先领访客
cookie、取值路径）全部提炼成配置字段——对方改版时改配置，不动逻辑。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..contracts import SourceType
from ..settings import DEFAULT_UA, SOURCES_DIR


class FieldSpec(BaseModel):
    """一个字段怎么从原始条目里取出来。

    三种写法：
      title: title                              # 直接取路径
      url:   { template: "…/{bvid}" }           # 用其它字段拼
      time:  { path: pubdate, type: unix }      # 取路径并转类型
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    template: str | None = Field(
        default=None, description="用 {字段路径} 占位；可加 |urlencode"
    )
    type: Literal["str", "int", "float", "unix", "iso"] = "str"
    default: Any = None

    @model_validator(mode="after")
    def _need_one_source(self) -> FieldSpec:
        if (self.path is None) == (self.template is None):
            raise ValueError("path 与 template 必须且只能给一个")
        return self


def _as_spec(v: Any) -> FieldSpec:
    if isinstance(v, str):
        return FieldSpec(path=v)
    if isinstance(v, dict):
        return FieldSpec(**v)
    raise TypeError(f"字段定义必须是字符串或映射，收到 {type(v).__name__}")


class RequestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    timeout: float = 10.0

    def merged_headers(self) -> dict[str, str]:
        headers = {"User-Agent": DEFAULT_UA, **self.headers}
        return headers


class PreRequestSpec(BaseModel):
    """先空跑一个请求领访客 cookie，再带着它调正式接口（抖音那招）。"""

    model_config = ConfigDict(extra="forbid")

    url: str
    method: Literal["GET", "POST"] = "GET"
    timeout: float = 10.0


class ExtractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["json"] = Field(
        default="json", description="v1 只支持 JSON；HTML/CSS 提取待热榜扩容时再加"
    )
    list: str = Field(default="", description="列表所在路径，空 = 根就是列表")
    fields: dict[str, FieldSpec]

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("fields"), dict):
            data = {**data, "fields": {k: _as_spec(v) for k, v in data["fields"].items()}}
        return data

    @model_validator(mode="after")
    def _need_required_fields(self) -> ExtractSpec:
        missing = {"native_id", "title", "url"} - set(self.fields)
        if missing:
            raise ValueError(f"extract.fields 缺必填项：{sorted(missing)}")
        return self


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="内部唯一标识，也是 meta.sources[].name")
    display_name: str = Field(description="人类可读源名，进 Item.source.name")
    type: SourceType = SourceType.HOTLIST
    platform: str | None = None
    enabled: bool = True
    min_interval: int = Field(
        default=300, ge=120, description="自适应抓取间隔下限（秒），最短 2 分钟"
    )
    lang: str | None = None
    categories: list[str] = Field(
        default_factory=list, description="源级分类，无条件打在该源所有条目上"
    )
    pre_request: PreRequestSpec | None = None
    request: RequestSpec
    extract: ExtractSpec

    @model_validator(mode="after")
    def _default_platform(self) -> SourceConfig:
        if self.platform is None and self.type is SourceType.HOTLIST:
            object.__setattr__(self, "platform", self.name)
        return self


def load_source(path: Path) -> SourceConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: 顶层必须是映射")
    try:
        return SourceConfig(**data)
    except Exception as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def load_sources(directory: Path | None = None) -> dict[str, SourceConfig]:
    """加载目录下全部源配置。一个配置坏了不静默跳过——启动即报错。"""
    directory = directory or SOURCES_DIR
    if not directory.exists():
        return {}
    sources: dict[str, SourceConfig] = {}
    for path in sorted(directory.glob("*.yaml")):
        cfg = load_source(path)
        if cfg.name in sources:
            raise ValueError(f"源名重复：{cfg.name}（{path.name}）")
        sources[cfg.name] = cfg
    return sources
