"""Tests for storage/keys.py."""

import uuid

from media_service.storage.keys import build_object_key


_OWNER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_MEDIA = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_TENANT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def test_key_without_tenant():
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="document",
        filename="report.pdf",
    )
    assert key == f"users/{_OWNER}/document/{_MEDIA}/original/report.pdf"


def test_key_with_tenant():
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="asset",
        filename="logo.png",
        tenant_id=_TENANT,
    )
    assert key == f"tenants/{_TENANT}/users/{_OWNER}/asset/{_MEDIA}/original/logo.png"


def test_key_strips_path_traversal_in_filename():
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="asset",
        filename="../../etc/passwd",
    )
    # The helper takes the last path segment only
    assert key.endswith("/passwd")


def test_key_normalises_category_spaces():
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category=" Chat Attachment ",
        filename="img.jpg",
    )
    assert "/chat_attachment/" in key


def test_key_normalises_backslash_in_filename():
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="document",
        filename=r"sub\dir\file.txt",
    )
    assert key.endswith("/file.txt")


def test_key_falls_back_when_filename_empties_after_strip():
    # "foo/" and a bare dot-reference both reduce to "" / "." / ".." after the
    # path-strip; the helper must still produce a real, unambiguous key.
    for filename in ("trailing/", "..", "."):
        key = build_object_key(
            owner_user_id=_OWNER,
            media_id=_MEDIA,
            category="document",
            filename=filename,
        )
        assert key == f"users/{_OWNER}/document/{_MEDIA}/original/file"
