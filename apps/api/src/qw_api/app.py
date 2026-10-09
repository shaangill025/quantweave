"""Application factory, configuration and the C-01 error envelope (T010).

Nothing reads the environment: the entry point passes `Settings`, a connection
provider and an authenticator to `create_app`. Every response has a server-made
`X-Correlation-Id`; every error body (validation and 500 included) is the
versioned RFC 9457 envelope of T008 review C-01. Request logs hold method, route
template, status and correlation id only, never headers, cookies or bodies.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from qw_adapters.tenancy import MAX_SESSION_TTL, Conn
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from qw_api.security import Authenticator

log = logging.getLogger("qw_api")
ConnectionProvider = Callable[[], AbstractContextManager[Conn]]
SAFE_METHODS = frozenset({"GET", "HEAD"})
_ORIGIN = re.compile(r"(https://[a-z0-9.-]+|http://(localhost|127\.0\.0\.1))(:\d+)?")
_REFERER = re.compile(_ORIGIN.pattern + r"(?=/|$)")


@dataclass(frozen=True, slots=True)
class Settings:
    """Explicit API configuration. `allowed_origins` are exact `scheme://host[:port]`
    strings (https, or http for localhost only)."""

    allowed_origins: frozenset[str]
    session_ttl: timedelta = timedelta(hours=12)
    step_up_window: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        if not self.allowed_origins or not all(
            _ORIGIN.fullmatch(o) for o in self.allowed_origins
        ):
            raise ValueError("allowed_origins must be exact https origins")
        if not timedelta(0) < self.session_ttl <= MAX_SESSION_TTL:
            raise ValueError("session_ttl must be in (0, 30 days]")
        if not timedelta(0) < self.step_up_window <= timedelta(hours=1):
            raise ValueError("step_up_window must be in (0, 1 hour]")


@dataclass(frozen=True, slots=True)
class FieldError:
    pointer: str
    code: str
    message: str


@dataclass
class ApiError(Exception):
    status: int
    code: str
    detail: str
    retryable: bool = False
    field_errors: tuple[FieldError, ...] = ()


def correlation_id(request: Request) -> str:
    return str(request.scope.get("state", {}).get("correlation_id", "unknown"))


def problem(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError)
    cid = correlation_id(request)
    body = {
        "type": f"/problems/{error.code}",
        "title": HTTPStatus(error.status).phrase,
        "status": error.status,
        "code": error.code,
        "detail": error.detail,
        "correlation_id": cid,
        "retryable": error.retryable,
        "retry_after_s": None,
        "field_errors": [
            {"pointer": f.pointer, "code": f.code, "message": f.message}
            for f in error.field_errors
        ],
        "reasons": [],
        "error_schema": "1",
    }
    return JSONResponse(
        body, error.status, {"X-Correlation-Id": cid, "Cache-Control": "no-store"},
        media_type="application/problem+json",
    )  # fmt: skip


def _pointer(loc: Iterable[Any]) -> str:
    parts = [str(p).replace("~", "~0").replace("/", "~1") for p in loc]
    return "/" + "/".join(parts[1:] if parts[:1] == ["body"] else parts)


async def _on_validation(request: Request, exc: Exception) -> JSONResponse:
    # Messages come from the validator's templates; the rejected input is not echoed.
    assert isinstance(exc, RequestValidationError)
    errors = tuple(
        FieldError(_pointer(e["loc"]), str(e["type"]), str(e["msg"]))
        for e in exc.errors()
    )
    detail = "The request does not match the contract."
    return problem(request, ApiError(400, "invalid_request", detail, False, errors))


async def _on_http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
    return problem(request, ApiError(exc.status_code, code, "No such operation."))


async def _on_crash(request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled %s cid=%s", type(exc).__name__, correlation_id(request))
    return problem(
        request, ApiError(500, "internal_error", "Unexpected failure.", True)
    )


def _origin_of(headers: dict[bytes, bytes]) -> str | None:
    origin = headers.get(b"origin")
    if origin is not None:
        return origin.decode("latin-1")
    referer = headers.get(b"referer")
    if referer is None:
        return None
    match = _REFERER.match(referer.decode("latin-1"))
    return match.group(0) if match else ""


class Envelope:
    """Pure ASGI middleware: correlation id, no-store, the request log line, and the
    Origin/Referer allowlist for every state-changing method (fail closed: a
    missing, `null` or foreign origin is rejected before routing)."""

    def __init__(self, app: ASGIApp, allowed_origins: frozenset[str]) -> None:
        self.app, self.allowed = app, allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cid = secrets.token_hex(16)
        scope.setdefault("state", {})["correlation_id"] = cid
        status = [500]

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                status[0] = message["status"]
                drop = {b"x-correlation-id", b"cache-control"}
                headers = [h for h in message.get("headers", []) if h[0] not in drop]
                headers += [(b"x-correlation-id", cid.encode()),
                            (b"cache-control", b"no-store")]  # fmt: skip
                message = {**message, "headers": headers}
            await send(message)

        try:
            if scope["method"] not in SAFE_METHODS and (
                _origin_of(dict(scope["headers"])) not in self.allowed
            ):
                error = ApiError(403, "origin_rejected", "Cross-origin request.")
                await problem(Request(scope), error)(scope, receive, send)
                status[0] = 403
            else:
                await self.app(scope, receive, tagged)
        finally:
            route = getattr(scope.get("route"), "path", "-")
            log.info("%s %s %s cid=%s", scope["method"], route, status[0], cid)


def create_app(
    settings: Settings, connections: ConnectionProvider, authenticator: Authenticator
) -> FastAPI:
    from qw_api.routes import router

    app = FastAPI(
        title="Portfolio Intelligence API", version="0.1.0", docs_url=None,
        redoc_url=None, openapi_url=None,
    )  # fmt: skip
    app.state.settings = settings
    app.state.connections = connections
    app.state.authenticator = authenticator
    app.include_router(router)
    app.add_exception_handler(ApiError, problem)
    app.add_exception_handler(RequestValidationError, _on_validation)
    app.add_exception_handler(HTTPException, _on_http)
    app.add_exception_handler(Exception, _on_crash)
    app.add_middleware(Envelope, allowed_origins=settings.allowed_origins)
    return app
