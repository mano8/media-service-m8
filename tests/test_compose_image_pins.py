"""Static compose-policy tests for image pinning (item 4.1).

These tests parse the YAML files directly — no running Docker required.

Policy:
  hardened_media_m8 — every service image must carry an explicit tag (no bare
  image names) and must NOT use `:latest` or an untagged reference.  Version
  tags without digests are acceptable (the test enforces the minimum bar;
  digest pinning is documented in the CHANGELOG as the recommended production
  upgrade path).

  dev_media_m8 — same no-bare-image / no-`:latest` rule; digest pinning is
  optional in dev.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_COMPOSE_DIR = Path(__file__).parent.parent / "docker_compose"
_HARDENED = _COMPOSE_DIR / "hardened_media_m8" / "docker-compose.yml"
_DEV = _COMPOSE_DIR / "dev_media_m8" / "docker-compose.yml"

# Matches a bare image name with no tag (e.g. "alpine", "quay.io/minio/minio").
_BARE_IMAGE_RE = re.compile(r"^[^:@]+$")
# Matches the :latest pseudo-tag.
_LATEST_RE = re.compile(r":latest$", re.IGNORECASE)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _service_images(compose: dict) -> list[tuple[str, str]]:
    """Return [(service_name, image_ref), ...] for every service that sets image:."""
    return [
        (name, svc["image"])
        for name, svc in compose.get("services", {}).items()
        if "image" in svc
    ]


# ---------------------------------------------------------------------------
# hardened_media_m8
# ---------------------------------------------------------------------------


class TestHardenedImagePins:
    """Every image in the hardened stack must be pinned (tag or digest)."""

    def _images(self) -> list[tuple[str, str]]:
        return _service_images(_load(_HARDENED))

    def test_no_bare_images(self):
        bare = [(svc, img) for svc, img in self._images() if _BARE_IMAGE_RE.match(img)]
        assert not bare, (
            "hardened_media_m8: the following services use bare (untagged) image "
            "references — pin each to a specific tag or digest:\n"
            + "\n".join(f"  {svc}: {img}" for svc, img in bare)
        )

    def test_no_latest_tag(self):
        latest = [(svc, img) for svc, img in self._images() if _LATEST_RE.search(img)]
        assert not latest, (
            "hardened_media_m8: the following services use `:latest` — "
            "pin to an immutable tag or digest:\n"
            + "\n".join(f"  {svc}: {img}" for svc, img in latest)
        )

    @pytest.mark.parametrize(
        "service,expected_prefix",
        [
            ("cert-init", "alpine:"),
            ("minio", "quay.io/minio/minio:RELEASE."),
            ("minio-init", "quay.io/minio/mc:RELEASE."),
        ],
    )
    def test_previously_bare_images_are_pinned(
        self, service: str, expected_prefix: str
    ):
        images = dict(self._images())
        img = images.get(service)
        assert img is not None, f"hardened_media_m8: service {service!r} not found"
        assert img.startswith(expected_prefix), (
            f"hardened_media_m8: {service!r} image {img!r} does not start with "
            f"{expected_prefix!r} — was it accidentally reverted to a bare reference?"
        )


# ---------------------------------------------------------------------------
# dev_media_m8
# ---------------------------------------------------------------------------


class TestDevImagePins:
    """Dev stack images must also carry explicit tags (no bare references)."""

    def _images(self) -> list[tuple[str, str]]:
        return _service_images(_load(_DEV))

    def test_no_bare_images(self):
        bare = [(svc, img) for svc, img in self._images() if _BARE_IMAGE_RE.match(img)]
        assert not bare, (
            "dev_media_m8: the following services use bare (untagged) image "
            "references — pin each to a specific tag:\n"
            + "\n".join(f"  {svc}: {img}" for svc, img in bare)
        )

    def test_no_latest_tag(self):
        latest = [(svc, img) for svc, img in self._images() if _LATEST_RE.search(img)]
        assert not latest, (
            "dev_media_m8: the following services use `:latest` — "
            "pin to an immutable tag:\n"
            + "\n".join(f"  {svc}: {img}" for svc, img in latest)
        )
