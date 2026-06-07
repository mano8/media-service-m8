"""
Alembic environment configuration module.

Handles database migrations in offline and online modes.
"""
# pylint: disable=invalid-name,too-many-arguments,too-few-public-methods
# pylint: disable=unused-import,consider-using-f-string,no-member
# pylint: disable=redefined-builtin,unused-argument

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

from media_service.core.config import settings
import media_service.db_models  # noqa: F401

# ---------------------------------------------------------------------
# PYTHONPATH (Docker / monorepo safe)
# ---------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# ---------------------------------------------------------------------
# ALEMBIC CONFIG
# ---------------------------------------------------------------------
config = context.config

if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

VERSION_TABLE = config.get_main_option("version_table")
VERSION_LOCATIONS = [config.get_main_option("version_locations")]


# ---------------------------------------------------------------------
# DATABASE URL
# ---------------------------------------------------------------------
def get_url() -> str:
    """Return database URL from settings."""
    return str(settings.SQLALCHEMY_DATABASE_URI)


# ---------------------------------------------------------------------
# INCLUDE OBJECT FILTER
# ---------------------------------------------------------------------
def include_object(
    object: Any,  # noqa: A002
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """
    Filter database objects included in migrations.
    """
    if type_ == "table":
        if name == VERSION_TABLE:
            return True
        return not reflected

    return True


# ---------------------------------------------------------------------
# RENDER ITEM — emit imports for media_service custom column types
# ---------------------------------------------------------------------
def render_item(type_: str, obj: Any, autogen_context: Any) -> bool:
    """Ensure custom column types emit their import in generated migrations.

    Alembic renders a user-defined type as ``module.Class(...)`` but does not
    add the matching ``import module`` line, which raises ``NameError`` at
    migration time (e.g. ``media_service.core.db_models.UUIDString``). Register
    the import for any media_service type, then fall back to default rendering.
    """
    if type_ == "type":
        module = obj.__class__.__module__
        if module.startswith("media_service"):
            autogen_context.imports.add(f"import {module}")
    return False


# ---------------------------------------------------------------------
# OFFLINE MIGRATIONS
# ---------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Run migrations in offline mode."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        include_object=include_object,
        render_item=render_item,
        version_table=VERSION_TABLE,
        version_locations=VERSION_LOCATIONS,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------
# ONLINE MIGRATIONS
# ---------------------------------------------------------------------
def run_migrations_online() -> None:
    """Run migrations in online mode."""
    configuration: dict[str, Any] = dict(
        config.get_section(config.config_ini_section) or {}
    )
    configuration["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            render_item=render_item,
            version_table=VERSION_TABLE,
            version_locations=VERSION_LOCATIONS,
        )

        with context.begin_transaction():
            context.run_migrations()


# ---------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
