"""CI workflow and Dockerfile policy tests (plan items 11.5 and 11.7).

Asserts immutable action references and a digest-pinned base image so the CI
and release pipelines cannot silently pull a changed layer or action.

Rules asserted here (11.5):
- The media_service/Dockerfile must use a digest-pinned FROM (@sha256:...) in
  both the builder and runtime stages so rebuilds cannot silently resolve a
  different base layer.

Rules asserted here (11.7):
- ci.yml must not exist — CI.yaml is the single canonical quality gate.
- CI.yaml must include a secret-scan (gitleaks) job.
- Every action reference in CI.yaml must be pinned to a full 40-char SHA.
- Every action reference in docker-publish.yaml must be pinned to a full 40-char SHA.

No Docker or network access required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_YAML = WORKFLOWS / "CI.yaml"
DOCKER_PUBLISH_YAML = WORKFLOWS / "docker-publish.yaml"
DOCKERFILE = REPO_ROOT / "media_service" / "Dockerfile"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_USES_RE = re.compile(r"uses:\s+([a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+@(\S+))")
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


def _action_refs(path: Path) -> list[tuple[str, str]]:
    """Return (full-ref, sha-candidate) for every action ``uses:`` in a workflow."""
    results: list[tuple[str, str]] = []
    for m in _USES_RE.finditer(path.read_text()):
        full_ref = m.group(1)
        sha_part = m.group(2).split("#")[0].strip()
        results.append((full_ref, sha_part))
    return results


# ---------------------------------------------------------------------------
# 11.5 — Dockerfile digest pinning
# ---------------------------------------------------------------------------


def test_dockerfile_from_stages_are_digest_pinned() -> None:
    """Both FROM stages in media_service/Dockerfile must use @sha256: digests."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith("FROM")
    ]
    assert from_lines, "No FROM lines found in Dockerfile"
    for line in from_lines:
        assert _DIGEST_RE.search(line), (
            f"Dockerfile FROM line is not digest-pinned: '{line}' — "
            "add @sha256:<digest> so rebuilds cannot silently resolve a different base layer"
        )


# ---------------------------------------------------------------------------
# 11.7 — CI workflow consolidation and immutable action policy
# ---------------------------------------------------------------------------


def test_no_duplicate_ci_yml() -> None:
    """ci.yml must not exist — CI.yaml is the single canonical quality gate."""
    assert not (WORKFLOWS / "ci.yml").exists(), (
        "Found duplicate ci.yml; the canonical workflow is CI.yaml."
    )


def test_ci_yaml_has_secret_scan_job() -> None:
    """CI.yaml must contain the gitleaks secret-scan job."""
    wf = _load_yaml(CI_YAML)
    assert "secret-scan" in wf["jobs"], "CI.yaml must include a secret-scan job."


def test_ci_yaml_actions_are_sha_pinned() -> None:
    """Every action reference in CI.yaml must be pinned to a full 40-char commit SHA."""
    refs = _action_refs(CI_YAML)
    assert refs, "No action references found in CI.yaml."
    for full_ref, sha_part in refs:
        assert _SHA_RE.match(sha_part), (
            f"CI.yaml: '{full_ref}' is not SHA-pinned — use a full 40-char commit hash."
        )


def test_docker_publish_yaml_actions_are_sha_pinned() -> None:
    """Every action reference in docker-publish.yaml must be pinned to a full 40-char SHA."""
    refs = _action_refs(DOCKER_PUBLISH_YAML)
    assert refs, "No action references found in docker-publish.yaml."
    for full_ref, sha_part in refs:
        assert _SHA_RE.match(sha_part), (
            f"docker-publish.yaml: '{full_ref}' is not SHA-pinned — use a full 40-char commit hash."
        )
