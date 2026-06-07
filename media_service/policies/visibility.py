"""Visibility policy helpers for media objects."""

from enum import StrEnum


class Visibility(StrEnum):
    """Generic visibility model for media objects."""

    PUBLIC = "public"
    PRIVATE = "private"
    TENANT = "tenant"
    SENSITIVE = "sensitive"
