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
    pattern: str | None = Field(
        default=None,
        description=(
            "取到值之后、转类型之前，先用这个正则抽出第一个捕获组。"
            "用于「想要的东西埋在一段更长的字符串里」——比如锚点 id 是 "
            "`2025-12-11-3`，日期只是它的前一段。"
        ),
    )
    type: Literal["str", "int", "float", "unix", "iso", "strptime", "slug"] = "str"
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


class StatusSpec(BaseModel):
    """响应体里的业务状态码在哪、什么值算成功。

    很多站点**永远回 HTTP 200**，真实结果藏在响应体里（B站的 `code`、
    公众平台的 `base_resp.ret`）。不声明这个，引擎只能看 HTTP 状态，
    于是「临时被风控挡了一下」会被误报成「对方改版了」——前者退避即可，
    后者要人去改配置，两者的处理方式完全相反。
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="状态码在响应体里的点分路径，如 code 或 base_resp.ret")
    ok: list[int] = Field(default_factory=lambda: [0], description="哪些值算成功")
    message_path: str | None = Field(
        default=None, description="错误说明的路径，用于把上游的原话带进日志"
    )
    #: 状态码 → 错误类型。没列出的一律当 UPSTREAM_DOWN。
    rate_limited: list[int] = Field(default_factory=list, description="判为限流的码")
    auth_expired: list[int] = Field(default_factory=list, description="判为凭据失效的码")
    captcha: list[int] = Field(default_factory=list, description="判为触发验证码的码")


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


class ChannelAccount(BaseModel):
    """channel 要订阅的一个账号。

    公众号那边强烈建议连 `fakeid` 一起写死，理由有两条，都不是优化而是正确性：

    1. **按名字搜会搜错号。** 实测搜「智谱AI」命中的是个 2022 年就停更的同名号，
       而智谱现在发内容的是「智谱清言」；搜「Kimi」命中的是 2018 年一个毫不
       相干的号。同名号、山寨号、停更旧号在公众平台上极常见。
    2. **省掉每轮的搜索请求。** 不给 fakeid 的话每个号每轮都要先搜一次，
       18 个号就是 18 次搜索——而搜索正是公众平台上最容易触发风控的动作。

    fakeid 怎么拿：`python -m sourcepilot.channels.wechat.lookup <名字>`。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    #: 公众平台的账号唯一标识。给了就直接用，跳过按名字搜索那一步。
    fakeid: str | None = None

    @classmethod
    def coerce(cls, value):
        """允许 YAML 里直接写字符串——老配置和只有名字的源不必改。"""
        return cls(name=value) if isinstance(value, str) else value


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
    max_items: int | None = Field(
        default=100,
        ge=1,
        description=(
            "单次采集最多取列表前多少条，None = 不限。默认 100。"
            "源给的顺序天然有意义——RSS 按时间倒序、榜单按名次——所以「取前 N 条」"
            "就是「取最新/最靠前的 N 条」。它挡的是 OpenAI 那种一次吐 1050 篇十年"
            "历史的 RSS：每 15 分钟重解析一遍全量，而其中新内容通常是 0 条。"
            "注意这只省解析与入库，不省下载——RSS 是整个文件，要省流量得靠条件请求。"
        ),
    )
    lang: str | None = None
    base_url: str = Field(
        default="", description="站点根地址；模板里用 {base_url} 把相对链接拼成绝对链接"
    )
    categories: list[str] = Field(
        default_factory=list, description="源级分类，无条件打在该源所有条目上"
    )
    channel: str | None = Field(
        default=None,
        description=(
            "改用重逻辑 channel 而不是声明式引擎（如 wechat、x）。"
            "声明式配置搞不定的源——需要登录态、签名、账号池的——单独写 Python，"
            "但仍走同一套调度、状态记录与降级路径，不另起一套。"
        ),
    )
    ranked: bool = Field(
        default=False,
        description=(
            "这个源本身是不是一份排行榜。只有 true 时 score 才由榜内位置换算；"
            "否则固定 0.0（契约 §2：无热度信号的源不许编一个热度出来）。"
            "默认 false——按时间倒序的 RSS/快讯没有名次可言，硬套排名等于把"
            "「第几个被列出来」伪装成「有多热」。"
        ),
    )
    accounts: list[ChannelAccount] = Field(
        default_factory=list,
        description=(
            "channel 专用：要订阅的账号列表。写字符串就是账号名；"
            "公众号建议写成 `{name, fakeid}`，见 ChannelAccount 的说明。"
        ),
    )
    backends: list[str] = Field(
        default_factory=list,
        description=(
            "channel 专用：后端降级链，按顺序试，前一个失败就换下一个。"
            "留空则用 channel 自己的默认顺序。"
        ),
    )
    nitter_instances: list[str] = Field(
        default_factory=list,
        description="channel 专用：自建 Nitter 实例，排在公共实例前面（公共实例寿命很短）",
    )
    per_account_limit: int = Field(
        default=10, ge=1, le=50, description="channel 专用：每个账号取多少条"
    )
    account_interval: float = Field(
        default=3.0,
        ge=0,
        description="channel 专用：账号之间的请求间隔（秒）。公众平台对连续请求很敏感",
    )
    verify_urls: bool = Field(
        default=False,
        description=(
            "逐条 HEAD 校验生成的 URL，404 的丢掉。"
            "只在 URL 是推导出来的（比如把标题 slug 化）时才开——"
            "推导规则是对站点的假设，开着它才能把假设失效变成可见的条目数下降，"
            "而不是悄悄产出一堆死链。"
        ),
    )
    status: StatusSpec | None = Field(
        default=None,
        description="响应体里的业务状态码。站点用 HTTP 200 + 体内错误码表示拒绝时必填",
    )
    pre_request: PreRequestSpec | None = None
    request: RequestSpec | None = None
    extract: ExtractSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_accounts(cls, data: Any) -> Any:
        """YAML 里既能写 `- 量子位`，也能写 `- {name: …, fakeid: …}`。"""
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            data = {**data, "accounts": [ChannelAccount.coerce(a) for a in data["accounts"]]}
        return data

    @model_validator(mode="after")
    def _need_backend(self) -> SourceConfig:
        if self.channel is None and (self.request is None or self.extract is None):
            raise ValueError("声明式源必须给 request 与 extract；重逻辑源用 channel 指定")
        return self

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
