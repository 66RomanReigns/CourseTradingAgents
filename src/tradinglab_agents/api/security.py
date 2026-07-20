from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from fastapi import Request
from fastapi.responses import JSONResponse, Response


PUBLIC_PATHS = frozenset(
    {
        "/",
        "/health",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)


def api_auth_enabled() -> bool:
    return bool(os.environ.get("TRADINGLAB_API_TOKEN", "").strip())


def _presented_token(request: Request) -> str:
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return api_key
    authorization = request.headers.get("authorization", "").strip()
    scheme, separator, value = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return value.strip()
    return ""


async def authenticate_request(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Protect every non-public endpoint with one local operator token.

    The token is read from the process environment on every request so a
    service restart is not required by tests that isolate environment values.
    No token value is ever included in responses or logs.
    """

    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    expected = os.environ.get("TRADINGLAB_API_TOKEN", "").strip()
    if not expected:
        return JSONResponse(
            status_code=503,
            content={
                "detail": "API authentication is not configured",
                "required_env": "TRADINGLAB_API_TOKEN",
            },
        )

    presented = _presented_token(request)
    if not presented or not hmac.compare_digest(presented, expected):
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content={"detail": "invalid or missing API token"},
        )
    return await call_next(request)
