from typing import Annotated

from fastapi import Depends

from media_service.core.deps import CurrentUser, SessionDep  # noqa: F401
from media_service.storage.client import ObjectStorage


def get_storage() -> ObjectStorage:
    """Provide an ObjectStorage instance (overridable in tests)."""
    return ObjectStorage()


StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
