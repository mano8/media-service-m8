"""Tests for core/db_models.py helpers."""

import uuid as _uuid

from media_service.core.db_models import UUIDString, prefixed_tables


def test_prefixed_tables_prepends_prefix():
    from media_service.core.config import settings

    assert prefixed_tables("foo") == f"{settings.TABLES_PREFIX}_foo"


def test_uuidstring_bind_param_none_returns_none():
    col = UUIDString()
    assert col.process_bind_param(None, None) is None


def test_uuidstring_bind_param_uuid_returns_string():
    col = UUIDString()
    uid = _uuid.uuid4()
    result = col.process_bind_param(uid, None)
    assert result == str(uid)


def test_uuidstring_result_value_none_returns_none():
    col = UUIDString()
    assert col.process_result_value(None, None) is None


def test_uuidstring_result_value_string_returns_uuid():
    col = UUIDString()
    uid = _uuid.uuid4()
    result = col.process_result_value(str(uid), None)
    assert result == uid
    assert isinstance(result, _uuid.UUID)
