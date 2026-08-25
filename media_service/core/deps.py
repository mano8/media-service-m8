"""Build-once site for auth and database dependencies.

Import ``auth``, ``engine``, the role-tier guards and ``SessionDep`` from here.
Never call ``build_auth_deps`` or ``create_db_engine`` a second time.

Role tiers
----------
``fastapi-m8`` builds the whole hierarchy inside ``build_auth_deps``; this
module only names the members the service exposes, so a route never has to
reach past ``app/deps.py`` to make an authorization decision. Each guard below
resolves the caller on the SDK's fresh, no-positive-cache user path and denies
with ``403`` through ``has_minimum_role`` — ``is_superuser`` alone never
satisfies a role threshold, so the flag cannot bypass a writer or admin guard.

The public read surface
-----------------------
Media is the one consumer whose lowest tier is *no tier at all*: a ``PUBLIC``
object is readable by a caller who never presented a token (A16 operator
decision). ``OptionalPrincipal`` is that floor — it resolves the bearer token
when one is presented and yields ``None`` when none is, so a handler can widen
what an anonymous caller sees without any route losing its identity when a
token *is* supplied.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlmodel import Session

from fastapi_m8 import (
    AuthDeps,
    DbEngine,
    UserModel,
    build_auth_deps,
    create_db_engine,
)

from .config import settings

# Single instances shared across the entire process.
auth: AuthDeps = build_auth_deps(settings)
engine: DbEngine = create_db_engine(settings)

get_current_user = auth.get_current_user
require_reader = auth.get_current_active_reader
require_writer = auth.get_current_active_writer
require_admin = auth.get_current_active_admin

#: Authenticated floor. Admits every role, ``USER`` included — the tier that may
#: read public items and nothing else. A route carrying this type is stating
#: "any authenticated principal, visibility filtered in the body", which is a
#: decision; a bare re-export of ``auth.CurrentUser`` would state nothing, which
#: is why there is no longer one (F7).
CurrentPrincipal = Annotated[UserModel, Depends(get_current_user)]
CurrentReader = Annotated[UserModel, Depends(require_reader)]
CurrentWriter = Annotated[UserModel, Depends(require_writer)]
CurrentAdmin = Annotated[UserModel, Depends(require_admin)]

# auto_error=False is the whole point: a missing Authorization header yields
# None instead of 401, which is what lets the public read surface answer an
# anonymous caller. A *malformed* or expired token still fails loudly below —
# presenting a broken credential is never silently downgraded to anonymous.
_optional_bearer = OAuth2PasswordBearer(
    tokenUrl=f"{settings.AUTH_PREFIX}/login/access-token",
    auto_error=False,
)


async def get_optional_user(
    token: Annotated[str | None, Depends(_optional_bearer)],
) -> UserModel | None:
    """Resolve the caller when a bearer token is presented, else ``None``.

    Delegates to the SDK-built ``get_current_user`` for every token that *is*
    presented, so an anonymous-capable route validates identity through exactly
    the same validator, revocation check and active-user gate as an
    authenticated one. Only the absence of a header is special-cased.
    """
    if not token:
        return None
    return await get_current_user(token)


#: Public floor. ``None`` for an anonymous caller; the resolved principal
#: otherwise. Every handler carrying this type is responsible for narrowing an
#: anonymous read to ``PUBLIC`` records (see ``controllers/objects.py``).
OptionalPrincipal = Annotated[UserModel | None, Depends(get_optional_user)]

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
