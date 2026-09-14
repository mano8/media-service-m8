"""`T30-close-deferred-flags` acceptance: nothing tracked in this repository
names MinIO where it means *the* storage backend.

The object-storage backend migration replaced MinIO with SeaweedFS 4.x (Garage
2.x as the validated fallback) and renamed the vocabulary after the protocol
(`S3_*`). Four flags survived the migration because each was written down and
handed to a later step that never closed it; this module is the grep that
keeps that from happening again. It walks every tracked text file and requires
each remaining `minio` mention to be one of:

* a **historical** reference — the old backend named as the thing that was
  replaced (`old MinIO block`, `MinIO → SeaweedFS`, `MinIO-era`, ...);
* a **runtime-data directory name** kept in `.gitignore`/`.dockerignore` for
  worktrees checked out before the migration (`minio/data/*`);
* an identifier owned by `media-sdk-m8` (`get_minio_client`), which this
  repository re-exports and cannot rename on its own.

Whole files that exist to describe the migration *from* MinIO are exempt
(`CHANGELOG.md`, the data-migration runbook and overlay, the security matrix,
the versioning evaluation). Everything else — a default, a compose secret id,
an env-file header, a README describing the running stack — must not.

The `MINIO_*` → `S3_*` deprecation shim was this scan's one documented code
exception until `3.0.0` removed it; `media_service/core/config.py` is now
scanned like every other file, and the only tracked `MINIO_*` names left are
the ones the retired-key tests assert are *refused*.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# Files whose subject IS the migration away from MinIO (history, runbooks).
_EXEMPT_FILES = {
    "CHANGELOG.md",
    "docker_compose/hardened_media_m8/DATA_MIGRATION_RUNBOOK.md",
    "docker_compose/hardened_media_m8/docker-compose.migration.yml",
    "docker_compose/hardened_media_m8/SECURITY_REGRESSION_MATRIX.md",
    "docker_compose/hardened_media_m8/VERSIONING_OBJECTLOCK_EVALUATION.md",
    "tests/test_compose_migration_overlay.py",
    "tests/test_no_retired_backend_references.py",
}
# Vendored third-party assets that happen to contain the word.
_EXEMPT_DIR_PARTS = ("grafana/data/plugins",)
_TEXT_SUFFIXES = {
    ".py",
    ".yml",
    ".yaml",
    ".toml",
    ".md",
    ".txt",
    ".sh",
    ".json",
    ".cfg",
    ".ini",
    ".example",
    ".env",
    ".template",
    ".lock",
    ".gitignore",
    ".dockerignore",
    "",
}

# `\bminio` (not `\bminio\b`): `minio_access_key` must still be caught, while a
# word that merely contains the letters (Spanish "dominio") must not.
_MENTION = re.compile(r"\bminio", re.IGNORECASE)

# A line may mention `minio` only if it also carries one of these markers.
_ALLOWED_CONTEXT = re.compile(
    "|".join(
        (
            # history: the old backend named as what was replaced
            r"\bold\b",
            r"\bretired\b",
            r"\bformer\b",
            r"\blegacy\b",
            r"\bdeprecated\b",
            r"MinIO-era",
            r"MinIO-specific",
            r"did for MinIO",
            r"MinIO's",
            r"MinIO ?→",
            r"->",
            r"→",
            r"drops the `minio`",
            r"`minio` is no longer",
            r"MinIO/`minio`",
            r"has no MINIO_API_CORS_ALLOW_ORIGIN",
            r"before T30",
            r"renamed",
            r"rename them",
            r"pre-existing minio_",
            r"MinIO baseline",
            r"frozen MinIO",
            r"were named minio_",
            r"PathPrefix\(/minio\)",
            r"legitimately survives",
            # runtime-data directory names kept for pre-migration worktrees
            r"minio/data",
            r"\*\*/minio/",
            # media-sdk-m8-owned identifier this repo only re-exports
            r"get_minio_client",
            # explicit retired-id lists in the policy tests
            r"_RETIRED_S3_SECRET_IDS",
            r"retired_minio_secret_id",
            r"retired `\{retired\}`",
        )
    ),
    re.IGNORECASE,
)


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_REPO,
            check=True,
            capture_output=True,
        ).stdout
        rels = [p for p in out.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - no git
        rels = [
            str(p.relative_to(_REPO)).replace("\\", "/")
            for p in _REPO.rglob("*")
            if p.is_file()
            and not any(
                part in {".git", ".venv", "node_modules", "__pycache__"}
                for part in p.parts
            )
        ]
    return [_REPO / rel for rel in rels]


def _scannable(path: Path) -> bool:
    rel = str(path.relative_to(_REPO)).replace("\\", "/")
    if rel in _EXEMPT_FILES:
        return False
    if any(part in rel for part in _EXEMPT_DIR_PARTS):
        return False
    return path.suffix in _TEXT_SUFFIXES or path.name.startswith(".")


def _offending_lines() -> list[str]:
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file() or not _scannable(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = str(path.relative_to(_REPO)).replace("\\", "/")
        for lineno, line in enumerate(text.splitlines(), 1):
            if not _MENTION.search(line) or _ALLOWED_CONTEXT.search(line):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    return offenders


def test_no_tracked_file_names_minio_as_the_current_backend():
    offenders = _offending_lines()
    assert not offenders, (
        "These lines name MinIO as if it were the storage backend this fleet "
        "runs. Either it is a leftover to fix, or a genuinely historical "
        "reference that needs one of the markers this test recognises:\n  "
        + "\n  ".join(offenders)
    )


def test_the_four_t30_flags_stay_closed():
    """The four specific regressions T30 closed, each pinned by name so a
    partial revert cannot hide behind the fuzzy line scan above."""
    from media_service.core.config import Settings

    stack = _REPO / "docker_compose" / "hardened_media_m8"
    prod = (stack / "docker-compose.production.yml").read_text(encoding="utf-8")
    garage_prod = (stack / "docker-compose.garage.production.yml").read_text(
        encoding="utf-8"
    )
    template = (stack / "garage" / "garage.toml.template").read_text(encoding="utf-8")

    # 1. the field default does not name the retired backend
    assert Settings.model_fields["S3_ENDPOINT"].default == "storage:8333"
    # 2. the production overlay's storage-credential secrets are S3-named
    assert "  minio_access_key:" not in prod and "  minio_secret_key:" not in prod
    assert "/run/secrets/s3_access_key" in prod and "/run/secrets/s3_secret_key" in prod
    # 3. GARAGE_RPC_SECRET has Docker-secret _FILE wiring
    assert "GARAGE_RPC_SECRET_FILE: /run/secrets/garage_rpc_secret" in garage_prod
    # 4. the Garage region is templated, not static
    assert 's3_region = "@S3_REGION@"' in template
