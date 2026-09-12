"""Bearer-token authentication for the /api/* routes.

`require_token`          - `Authorization: Bearer <APP_TOKEN>` (scheme is case-insensitive).
`require_token_or_query` - same, but also accepts `?token=<APP_TOKEN>` for plain download links.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import HTTPException, Query, Request

from .config import settings


def bearer_from_header(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if not auth:
        return None
    scheme, _, token = auth.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def check_token(token: str | None) -> None:
    expected = settings.app_token or ""  # read lazily: tests swap the token before create_app()
    if not token or not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="unauthorized")


def require_token(request: Request) -> None:
    """Dependency: valid Bearer header or 401 {"detail": "unauthorized"}."""
    check_token(bearer_from_header(request))


def require_token_or_query(request: Request, token: Optional[str] = Query(default=None)) -> None:
    """Dependency for download endpoints: Bearer header OR `?token=` query parameter."""
    check_token(bearer_from_header(request) or token)
