"""Re-export public dependencies consumed by route modules.

The tier vocabulary is fixed here so a route module cannot reach past it and
invent its own authorization decision:

``OptionalPrincipal``
    Public floor — ``None`` when the caller presented no token. Carried only by
    the public read surface; the handler narrows an anonymous read to ``PUBLIC``
    records.
``CurrentPrincipal``
    Authenticated floor. Every role, ``USER`` included.
``CurrentReader``
    Owned reads — lists and items the caller owns.
``CurrentWriter``
    Mutations — upload, add, edit and delete of owned records.
``CurrentAdmin``
    Administrative surface. No route carries it today: ``/v1/admin`` stays on
    ``auth.get_current_active_superuser`` (A16 operator decision). It is
    exported so the tier exists in one place the day a route needs it, rather
    than being invented at the call site.

There is deliberately no bare ``CurrentUser`` export (A16/F7).
"""

__all__ = [
    "CurrentAdmin",
    "CurrentPrincipal",
    "CurrentReader",
    "CurrentWriter",
    "OptionalPrincipal",
    "SessionDep",
    "StorageDep",
    "get_current_user",
    "get_storage",
    "require_admin",
    "require_reader",
    "require_writer",
]

from typing import Annotated

from fastapi import Depends

from media_service.core.deps import CurrentAdmin as CurrentAdmin
from media_service.core.deps import CurrentPrincipal as CurrentPrincipal
from media_service.core.deps import CurrentReader as CurrentReader
from media_service.core.deps import CurrentWriter as CurrentWriter
from media_service.core.deps import OptionalPrincipal as OptionalPrincipal
from media_service.core.deps import SessionDep as SessionDep
from media_service.core.deps import get_current_user as get_current_user
from media_service.core.deps import require_admin as require_admin
from media_service.core.deps import require_reader as require_reader
from media_service.core.deps import require_writer as require_writer
from media_service.storage.client import ObjectStorage, get_storage_config


def get_storage() -> ObjectStorage:
    """Provide an ObjectStorage instance (overridable in tests)."""
    return ObjectStorage(get_storage_config())


StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
