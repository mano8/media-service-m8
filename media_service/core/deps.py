"""Build-once site for auth and database dependencies.

Import ``auth``, ``engine``, ``CurrentUser``, and ``SessionDep`` from here.
Never call ``build_auth_deps`` or ``create_db_engine`` a second time.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session

from fastapi_m8 import AuthDeps, DbEngine, build_auth_deps, create_db_engine

from .config import settings

# Single instances shared across the entire process.
auth: AuthDeps = build_auth_deps(settings)
engine: DbEngine = create_db_engine(settings)

CurrentUser = auth.CurrentUser
get_current_user = auth.get_current_user
get_db = engine.session_dep
SessionDep = Annotated[Session, Depends(get_db)]

_BEARER_PREFIX = "Bearer "


def require_service_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Authorize an internal (service-to-service) call via a shared bearer token.

    The worker presents ``Authorization: Bearer <token>``; the token is compared
    to ``MEDIA_INTERNAL_SERVICE_TOKEN`` in constant time. Any missing, malformed,
    or mismatched token raises 403 — these endpoints are never user-facing.
    """
    expected = settings.MEDIA_INTERNAL_SERVICE_TOKEN.get_secret_value()
    provided = ""
    if authorization and authorization.startswith(_BEARER_PREFIX):
        provided = authorization[len(_BEARER_PREFIX) :]
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid service token.",
        )


ServiceAuthDep = Annotated[None, Depends(require_service_token)]
