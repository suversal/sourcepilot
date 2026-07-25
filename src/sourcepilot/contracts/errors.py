"""结构化错误码。供 Agent 分支决策，见 docs/contract.md §1。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_DOWN = "UPSTREAM_DOWN"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    CAPTCHA = "CAPTCHA"
    NOT_FOUND = "NOT_FOUND"
    BAD_REQUEST = "BAD_REQUEST"
    TIMEOUT = "TIMEOUT"
    INTERNAL = "INTERNAL"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL: 500,
    ErrorCode.UPSTREAM_DOWN: 502,
    ErrorCode.CAPTCHA: 502,
    ErrorCode.AUTH_EXPIRED: 503,
    ErrorCode.TIMEOUT: 504,
}

#: 这些错误应先尝试降级回缓存，缓存也空时才对外报错。
DEGRADABLE: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_DOWN,
        ErrorCode.AUTH_EXPIRED,
        ErrorCode.CAPTCHA,
        ErrorCode.TIMEOUT,
    }
)


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str = Field(description="人类可读说明；不得包含账号、cookie 等内部细节")


class SourcePilotError(Exception):
    """内部异常，出口层统一翻译成信封。"""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    @property
    def degradable(self) -> bool:
        return self.code in DEGRADABLE

    def to_body(self) -> ErrorBody:
        return ErrorBody(code=self.code, message=self.message)


class BadRequest(SourcePilotError):
    code = ErrorCode.BAD_REQUEST


class NotFound(SourcePilotError):
    code = ErrorCode.NOT_FOUND


class RateLimited(SourcePilotError):
    code = ErrorCode.RATE_LIMITED


class UpstreamDown(SourcePilotError):
    code = ErrorCode.UPSTREAM_DOWN


class AuthExpired(SourcePilotError):
    """账号失效。对外只表示平台侧暂不可用，不暴露是哪个账号。"""

    code = ErrorCode.AUTH_EXPIRED


class Captcha(SourcePilotError):
    code = ErrorCode.CAPTCHA


class Timeout(SourcePilotError):
    code = ErrorCode.TIMEOUT
