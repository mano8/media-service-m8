from typing import Annotated

from fastapi import Depends

from media_service.core.deps import CurrentUser, SessionDep  # noqa: F401
from media_service.storage.client import ObjectStorage, get_storage_config


def get_storage() -> ObjectStorage:
    """Provide an ObjectStorage instance (overridable in tests)."""
    return ObjectStorage(get_storage_config())


StorageDep = Annotated[ObjectStorage, Depends(get_storage)]
