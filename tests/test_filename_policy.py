"""Tests for the portable-filename policy (``core/validation``).

A client-supplied filename reaches five sinks (object key, ``Content-
Disposition``, zip entry, listing, the client's own filesystem). The policy on
what a name may contain is decided once at the trust boundary; these tests pin
the rule set, the two API boundaries that refuse (upload initiate, metadata
update); the manifest-import boundary normalises instead and is pinned
next to the other archive-import cases in ``test_transfer_import.py``.
"""

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from media_service.core.validation import (
    FALLBACK_FILENAME,
    FORBIDDEN_FILENAME_CHARS,
    MAX_FILENAME_LENGTH,
    is_forbidden_filename_char,
    sanitize_filename,
    validate_filename,
)
from media_service.db_models.media_objects import MediaVisibility
from media_service.storage.keys import build_object_key
from media_service.storage.presign import _safe_content_disposition

from tests.test_objects import _make_object

_BACKSLASH = chr(92)
_NUL = chr(0)
_CRLF = chr(13) + chr(10)
_DEL = chr(0x7F)
_RTL_OVERRIDE = chr(
    0x202E
)  # bidi override: "photo<U+202E>gnp.exe" shows as photo.exe.png
_ZWSP = chr(0x200B)  # zero-width space (Cf)
_PRIVATE_USE = chr(0xE000)  # Co
_UNASSIGNED = chr(0x0378)  # Cn (as of Unicode 16)


# ── the rule set ──────────────────────────────────────────────────────────────


def test_forbidden_set_is_the_portable_filename_set():
    assert FORBIDDEN_FILENAME_CHARS == frozenset("/" + _BACKSLASH + '<>:"|?*')


@pytest.mark.parametrize(
    "ch",
    [
        "/",
        _BACKSLASH,
        "<",
        ">",
        ":",
        '"',
        "|",
        "?",
        "*",
        _NUL,
        chr(13),
        chr(10),
        chr(9),
        _DEL,
        _RTL_OVERRIDE,
        _ZWSP,
        _PRIVATE_USE,
        _UNASSIGNED,
    ],
    ids=[
        "slash",
        "backslash",
        "lt",
        "gt",
        "colon",
        "quote",
        "pipe",
        "question",
        "star",
        "nul",
        "cr",
        "lf",
        "tab",
        "del",
        "rtl-override",
        "zwsp",
        "private-use",
        "unassigned",
    ],
)
def test_forbidden_characters(ch: str):
    assert is_forbidden_filename_char(ch)


@pytest.mark.parametrize(
    "ch", [";", "%", "#", " ", "'", "(", "&", "+", "=", "@", "é", "日", "😀"]
)
def test_allowed_characters(ch: str):
    # Legitimate and common; the sinks that care (the URL path, the header)
    # encode them themselves.
    assert not is_forbidden_filename_char(ch)


# ── validate_filename (reject side) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("  photo #1 100%; ok.png  ", "photo #1 100%; ok.png"),
        ("été.png", "été.png"),
        ("a" * MAX_FILENAME_LENGTH, "a" * MAX_FILENAME_LENGTH),
    ],
)
def test_validate_accepts_and_trims(raw: str, expected: str):
    assert validate_filename(raw) == expected


def test_validate_normalises_to_nfc():
    decomposed = "e" + chr(0x0301) + ".png"  # e + combining acute
    assert validate_filename(decomposed) == "é.png"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        "..",
        "...",
        "a/b.png",
        "a" + _BACKSLASH + "b.png",
        "../../etc/passwd",
        "x" + _CRLF + "X-Injected: 1.png",
        "a" + _NUL + "b",
        "photo" + _RTL_OVERRIDE + "gnp.exe",
        "what?.png",
        'say "hi".png',
        "a:b.png",
        "a|b",
        "a*b",
        "<script>.png",
        "a" * (MAX_FILENAME_LENGTH + 1),
    ],
    ids=[
        "empty",
        "blank",
        "dot",
        "dotdot",
        "dots",
        "slash",
        "backslash",
        "traversal",
        "crlf-header",
        "nul",
        "rtl-override",
        "question",
        "quote",
        "colon",
        "pipe",
        "star",
        "angle",
        "too-long",
    ],
)
def test_validate_rejects(raw: str):
    with pytest.raises(ValueError):
        validate_filename(raw)


def test_validate_error_names_the_offending_characters():
    with pytest.raises(ValueError, match="'<' '>'"):
        validate_filename("<script>.png")


# ── sanitize_filename (normalise side) ───────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, FALLBACK_FILENAME),
        ("", FALLBACK_FILENAME),
        ("...", FALLBACK_FILENAME),
        ("trailing/", FALLBACK_FILENAME),
        ("../../etc/passwd", "passwd"),
        ("sub" + _BACKSLASH + "dir" + _BACKSLASH + "file.txt", "file.txt"),
        ("photo" + _RTL_OVERRIDE + "gnp.exe", "photo_gnp.exe"),
        ("x" + _CRLF + "y.png", "x__y.png"),
        ("what?.png", "what_.png"),
        # `/` is a separator: only the last segment survives, then `>` goes.
        ('<b>"hi"</b>.png', "b_.png"),
        ("  spaced .png ", "spaced .png"),
        ("a" * 300 + ".png", "a" * MAX_FILENAME_LENGTH),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "..",
        "a/b",
        "photo" + _RTL_OVERRIDE + "gnp.exe",
        "a" * 300,
        '<>:"|?*',
        _NUL * 5,
        " . ",
    ],
)
def test_sanitize_output_always_validates(raw):
    assert validate_filename(sanitize_filename(raw)) == sanitize_filename(raw)


# ── every sink stays safe for a name the policy admits ───────────────────────


def test_admitted_name_is_route_and_header_safe():
    name = validate_filename("photo #1 100%; ok's (é).png")
    key = build_object_key(
        owner_user_id=uuid.uuid4(),
        media_id=uuid.uuid4(),
        category="asset",
        filename=name,
    )
    # `#`, `%` and `;` are admitted by the policy and mapped by the key sink.
    assert key.endswith("/original/photo _1 100__ ok's (é).png")
    header = _safe_content_disposition(name)
    assert chr(13) not in header and chr(10) not in header
    assert header.startswith('attachment; filename="')


# ── API boundaries ────────────────────────────────────────────────────────────

_INITIATE = {
    "category": "document",
    "visibility": "private",
    "mime_type": "application/pdf",
    "expected_size_bytes": 2048,
}


@pytest.mark.parametrize(
    "bad",
    [
        "../x.pdf",
        "a" + _BACKSLASH + "b.pdf",
        "x" + _CRLF + "y.pdf",
        "photo" + _RTL_OVERRIDE + "fdp.exe",
        "<script>.pdf",
        "what?.pdf",
        "",
    ],
    ids=[
        "traversal",
        "backslash",
        "crlf",
        "rtl-override",
        "angle",
        "question",
        "empty",
    ],
)
def test_initiate_upload_refuses_a_forbidden_filename(
    client: TestClient, mock_storage: MagicMock, bad: str
):
    resp = client.post(
        "/media/v1/uploads/initiate", json={**_INITIATE, "original_filename": bad}
    )
    assert resp.status_code == 422, resp.text
    mock_storage.presigned_post_object.assert_not_called()


def test_initiate_upload_accepts_and_trims_a_portable_filename(
    client: TestClient, mock_storage: MagicMock, session: Session
):
    mock_storage.presigned_post_object.return_value = ("https://storage/b", {})
    resp = client.post(
        "/media/v1/uploads/initiate",
        json={**_INITIATE, "original_filename": "  report #1 (final).pdf "},
    )
    assert resp.status_code == 200, resp.text
    _, kwargs = mock_storage.presigned_post_object.call_args
    assert kwargs["object_key"].endswith("/original/report _1 (final).pdf")


@pytest.mark.parametrize("bad", ["../x.pdf", "<img>.pdf", "a:b.pdf", ""])
def test_update_object_refuses_a_forbidden_filename(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user, bad
):
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    resp = client.patch(f"/media/v1/objects/{obj.id}", json={"original_filename": bad})
    assert resp.status_code == 422, resp.text
    session.refresh(obj)
    assert obj.original_filename == "file.pdf"


def test_update_object_accepts_a_portable_filename(
    client: TestClient, mock_storage: MagicMock, session: Session, current_user
):
    obj = _make_object(session, current_user.id, visibility=MediaVisibility.PRIVATE)
    resp = client.patch(
        f"/media/v1/objects/{obj.id}", json={"original_filename": " renamed é.pdf "}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["original_filename"] == "renamed é.pdf"
