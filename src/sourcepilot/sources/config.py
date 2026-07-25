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

    JSON / RSS 用 path，HTML 用 select，三种格式都能用 template 拼：
      title: title                              # 取路径（JSON/RSS）
      title: { select: "a.t" }                  # 取选中元素的文本（HTML）
      url:   { select: "a.t", attr: href }      # 取选中元素的属性（HTML）
      url:   { template: "{base_url}{path}" }   # 用已抽出的字段拼
      time:  { path: pubdate, type: unix }      # 取路径并转类型
    """

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    select: str | None = Field(
        default=None, description="CSS 选择器，相对当前行；'.' 表示行元素自身"
    )
    attr: str | None = Field(
        default=None, description="配合 select：取该属性；不给则取文本"
    )
    template: str | None = Field(
        default=None, description="用 {字段名或路径} 占位；可加 |urlencode"
    )
    type: Literal["str", "int", "float", "unix", "iso", "strptime"] = "str"
    format: str | None = Field(
        default=None,
        description="配合 type=strptime 的时间格式，如 '%b %d, %Y'（网页上的人类可读日期）",
    )
    default: Any = None

    @model_validator(mode="after")
    def _need_one_source(self) -> FieldSpec:
        given = [self.path is not None, self.select is not None, self.template is not None]
        if sum(given) != 1:
            raise ValueError("path / select / template 必须且只能给一个")
        if self.attr is not None and self.select is None:
            raise ValueError("attr 只能配合 select 使用")
        if (self.type == "strptime") != (self.format is not None):
            raise ValueError("type=strptime 与 format 必须成对出现")
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
    impersonate: str | None = Field(
        default=None,
        description=(
            "TLS/JA3 指纹伪装档位（curl_cffi 的 impersonate 值，如 safari、chrome131）。"
            "只在对方用 Cloudflare 拦 TLS 握手时才需要——换 UA 解决不了那种拦截。"
            "哪个档位管用得实测，不同站点结论不同。"
        ),
    )

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
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    format: Literal["json", "html", "rss"] = "json"
    # 属性名不能叫 list——那会遮蔽内建 list，害得 pydantic 解析不了后面的类型注解。
    # YAML 里仍然写 `list:`，靠别名对上。
    rows: str = Field(
        default="",
        alias="list",
        description="JSON：列表所在路径，空 = 根即列表；HTML：行的 CSS 选择器；RSS：忽略",
    )
    fields: dict[str, FieldSpec] = Field(
        default_factory=dict, description="RSS 有默认映射，可留空"
    )
    exclude_if: dict[str, list[str]] = Field(
        default_factory=dict,
        description="字段值命中任一关键词就丢弃该条，用于剔广告；如 {title: [优惠, 补贴]}",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("fields"), dict):
            data = {**data, "fields": {k: _as_spec(v) for k, v in data["fields"].items()}}
        return data

    @model_validator(mode="after")
    def _check_shape(self) -> ExtractSpec:
        if self.format == "rss":
            # RSS 条目形状稳定，配置可以整个留空走默认映射。
            return self

        missing = {"native_id", "title", "url"} - set(self.fields)
        if missing:
            raise ValueError(f"extract.fields 缺必填项：{sorted(missing)}")

        if self.format == "html":
            if not self.rows:
                raise ValueError("format=html 必须给 extract.list（行的 CSS 选择器）")
            bad = [k for k, v in self.fields.items() if v.path is not None]
            if bad:
                raise ValueError(f"format=html 的字段要用 select 而非 path：{sorted(bad)}")
        else:
            bad = [k for k, v in self.fields.items() if v.select is not None]
            if bad:
                raise ValueError(f"format=json 的字段要用 path 而非 select：{sorted(bad)}")
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
    base_url: str = Field(
        default="", description="站点根地址；模板里用 {base_url} 把相对链接拼成绝对链接"
    )
    categories: list[str] = Field(
        default_factory=list, description="源级分类，无条件打在该源所有条目上"
    )
    pre_request: PreRequestSpec | None = None
    request: RequestSpec
    extract: ExtractSpec

    @model_validator(mode="after")
    def _default_platform(self) -> SourceConfig:
        if self.platform is None:
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
