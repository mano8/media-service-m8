"""Tests for policies/ownership.py and policies/visibility.py."""

import uuid

from media_service.policies.ownership import is_owner
from media_service.policies.visibility import Visibility


def test_is_owner_same_id_returns_true():
    uid = uuid.uuid4()
    assert is_owner(owner_user_id=uid, current_user_id=uid) is True


def test_is_owner_different_id_returns_false():
    assert (
        is_owner(
            owner_user_id=uuid.uuid4(),
            current_user_id=uuid.uuid4(),
        )
        is False
    )


def test_visibility_public():
    assert Visibility.PUBLIC == "public"


def test_visibility_private():
    assert Visibility.PRIVATE == "private"


def test_visibility_tenant():
    assert Visibility.TENANT == "tenant"


def test_visibility_sensitive():
    assert Visibility.SENSITIVE == "sensitive"
