"""Database model helpers."""

import uuid as _uuid

from sqlalchemy import CHAR, TypeDecorator

from media_service.core.config import settings


def prefixed_tables(name: str) -> str:
    """Return a table name prefixed with the configured TABLES_PREFIX."""
    return f"{settings.TABLES_PREFIX}_{name}"


class UUIDString(TypeDecorator):
    """CHAR(36) column that accepts uuid.UUID objects on bind and returns them on load.

    Needed for SQLite compatibility: SQLite does not auto-coerce uuid.UUID to
    VARCHAR, so parameter binding raises without this decorator.
    """

    impl = CHAR(36)
    cache_ok = True

    def process_bind_param(self, value: _uuid.UUID | str | None, dialect) -> str | None:
        if value is None:
            return None
        return str(value)

    def process_result_value(self, value: str | None, dialect) -> _uuid.UUID | None:
        if value is None:
            return None
        return _uuid.UUID(value)
