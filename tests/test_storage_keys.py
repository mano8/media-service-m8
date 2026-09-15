"""Tests for storage/keys.py."""

import uuid
from pathlib import Path

import pytest
import yaml

from media_service.storage.keys import build_object_key, build_variant_key


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


# ── route-safe key segment ────────────────────────────────────────────────────
#
# The hardened stack's Traefik answers 400 to a path carrying an encoded ;%?#
# or NUL (`traefik.yml` `encodedCharacters`), and a presigned GET puts the
# object key in the path — so a key carrying one of them uploads fine (the
# key is a POST form field) but every download/share link for it is dead.
# The served filename comes from `original_filename`, never from the key.


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("t24;v2.png", "t24_v2.png"),
        ("t24 100%.png", "t24 100_.png"),
        ("t24 what?.png", "t24 what_.png"),
        ("t24 #1.png", "t24 _1.png"),
        (f"a{chr(0)}b.png", "a_b.png"),
        (f"a{chr(13)}{chr(10)}b.png", "a__b.png"),
        (f"a{chr(0x7F)}b.png", "a_b.png"),
        (";%?#", "____"),
    ],
)
def test_key_replaces_characters_the_route_rejects(filename: str, expected: str):
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="asset",
        filename=filename,
    )
    assert key == f"users/{_OWNER}/asset/{_MEDIA}/original/{expected}"


def test_key_keeps_characters_the_route_accepts():
    # Space, quote, colon, unicode: percent-encoded by the client too, but the
    # proxy forwards them (probed live in T24) — leave them alone so the key
    # stays as close to the original name as the route allows.
    key = build_object_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="asset",
        filename='my "photo": été (1).png',
    )
    assert key.endswith('/original/my "photo": été (1).png')


def test_variant_key_uses_the_same_route_safe_segment():
    key = build_variant_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="asset",
        variant_name="thumb",
        filename="a;b%c?d#e.webp",
    )
    assert key.endswith("/variants/thumb/a_b_c_d_e.webp")


_TRAEFIK_STATIC_CONFS = sorted(
    (Path(__file__).parent.parent / "docker_compose").glob("*/traefik/traefik.yml")
)

# What each `allowEncoded<Name>: false` in Traefik's `encodedCharacters` refuses
# on the wire, and how `keys.py` keeps the character out of a key: "/" and "\\"
# by the path strip in `_safe_filename`, the rest by `_key_segment`.
_ENCODED_FLAG_TO_CHAR = {
    "allowEncodedSlash": "/",
    "allowEncodedBackSlash": "\\",
    "allowEncodedNullCharacter": chr(0),
    "allowEncodedSemicolon": ";",
    "allowEncodedPercent": "%",
    "allowEncodedQuestionMark": "?",
    "allowEncodedHash": "#",
}


@pytest.mark.parametrize(
    "conf",
    _TRAEFIK_STATIC_CONFS,
    ids=[c.parent.parent.name for c in _TRAEFIK_STATIC_CONFS],
)
def test_key_never_carries_a_character_traefik_refuses_encoded(conf: Path):
    """Couples `keys.py` to the shipped `encodedCharacters` policy.

    If a stack tightens the policy with a flag this table does not know, the
    test fails here rather than as a 400 on a customer's download link.
    """
    entrypoints = yaml.safe_load(conf.read_text(encoding="utf-8"))["entryPoints"]
    refused: set[str] = set()
    for entrypoint in entrypoints.values():
        flags = (entrypoint.get("http") or {}).get("encodedCharacters") or {}
        for flag, allowed in flags.items():
            assert flag in _ENCODED_FLAG_TO_CHAR, f"{conf}: unknown flag {flag}"
            if allowed is False:
                refused.add(_ENCODED_FLAG_TO_CHAR[flag])
    assert refused, f"{conf}: no encodedCharacters hardening found"
    for ch in refused:
        key = build_object_key(
            owner_user_id=_OWNER,
            media_id=_MEDIA,
            category="asset",
            filename=f"x{ch}y.png",
        )
        segment = key.rsplit("/original/", 1)[1]
        assert ch not in segment, (conf, repr(ch), segment)


# ── build_variant_key ─────────────────────────────────────────────────────────


def test_variant_key_without_tenant():
    key = build_variant_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="document",
        variant_name="web_webp",
        filename="web_webp.webp",
    )
    assert key == (f"users/{_OWNER}/document/{_MEDIA}/variants/web_webp/web_webp.webp")


def test_variant_key_with_tenant_and_normalisation():
    key = build_variant_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category=" Chat Attachment ",
        variant_name="Big Thumb",
        filename="../x/out.webp",
        tenant_id=_TENANT,
    )
    assert key == (
        f"tenants/{_TENANT}/users/{_OWNER}/chat_attachment/"
        f"{_MEDIA}/variants/big_thumb/out.webp"
    )


def test_variant_key_falls_back_when_filename_empties():
    key = build_variant_key(
        owner_user_id=_OWNER,
        media_id=_MEDIA,
        category="asset",
        variant_name="thumb_webp",
        filename="dir/",
    )
    assert key.endswith("/variants/thumb_webp/file")
