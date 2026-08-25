"""Tenant extraction from an authenticated principal.

One definition of *which tenant is this caller in*, shared by every surface that
scopes rows by tenant (`D4`). It previously existed twice — in
``controllers/objects.py`` and ``core/presets.py`` — and
``controllers/category.py`` imported the first, which pinned the category
controller to the objects controller. `U4`'s assignment step needs that
dependency the other way round (the objects controller resolves category ids on
PATCH), so the helper moved here, where neither controller has to import the
other and no third copy has to appear.

It holds no session and runs no query on purpose: scoping a query is the
caller's job, and this is only the claim lookup.
"""

import uuid

from fastapi_m8 import UserModel


def user_tenant_id(current_user: UserModel) -> uuid.UUID | None:
    """Return the caller's tenant as a UUID, or ``None`` when untenanted.

    ``UserModel`` does not (yet) carry a tenant claim, so this reads it
    defensively: callers without a tenant get ``None``, which never matches a
    ``TENANT`` object (see
    :func:`media_service.controllers.objects.require_visibility_access`).
    """
    raw = getattr(current_user, "tenant_id", None)
    return uuid.UUID(str(raw)) if raw is not None else None
