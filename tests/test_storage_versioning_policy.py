"""Guard for the `T28-versioning-objectlock-eval` recommendation (Wave 5).

`docker_compose/hardened_media_m8/VERSIONING_OBJECTLOCK_EVALUATION.md`
recommends **no adoption** of object versioning or Object Lock on any media
bucket, because `ObjectStorage.remove_object` issues an unversioned
`DeleteObject` and every deletion this service performs goes through it — so on
a versioned bucket the scheduled hard purge would report success while
reclaiming nothing (evaluation §1). The same operations are listed in
`FORBIDDEN_OPERATIONS` in `media-sdk-m8/tests/conformance/contract.py` and are
asserted negatively there against the SDK surface.

These tests hold the other half of that line, inside this repository: no
bootstrap in either storage profile, and no service code path, may start
issuing versioning / Object Lock / lifecycle calls while the recommendation
stands. If adoption is decided, the evaluation's §7 preconditions — a
version-aware delete contract in the SDK, an app-side reaper for noncurrent
versions, a workspace decision on Garage fallback parity — land first, and this
suite is updated with them rather than deleted.

Static and docker-free, like `test_compose_garage_profile.py` and
`test_compose_storage_policy.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_STACK = _REPO / "docker_compose" / "hardened_media_m8"
_BASE = _STACK / "docker-compose.yml"
_GARAGE = _STACK / "docker-compose.garage.yml"
_EVALUATION = _STACK / "VERSIONING_OBJECTLOCK_EVALUATION.md"
_SERVICE_PKG = _REPO / "media_service"

#: S3 operations the evaluation recommends against, in the spellings they would
#: actually appear in: the aws-cli subcommand a bootstrap one-shot would run,
#: and the botocore method name service code would call.
_FORBIDDEN_CLI = (
    "put-bucket-versioning",
    "put-object-lock-configuration",
    "put-object-retention",
    "put-object-legal-hold",
    "put-bucket-lifecycle-configuration",
)
_FORBIDDEN_PY = (
    "put_bucket_versioning",
    "put_object_lock_configuration",
    "put_object_retention",
    "put_object_legal_hold",
    "put_bucket_lifecycle_configuration",
)

#: Garage's own CLI equivalents, for the alternate profile. Garage 2.3.0
#: answers NotImplemented to all three S3 operations (evaluation M24/M25), so
#: there is nothing to call — this keeps it that way by name too.
_FORBIDDEN_GARAGE_CLI = ("bucket versioning", "object-lock")


def _service_sources() -> list[Path]:
    return sorted(p for p in _SERVICE_PKG.rglob("*.py") if "__pycache__" not in p.parts)


@pytest.mark.parametrize("compose", (_BASE, _GARAGE), ids=("seaweedfs", "garage"))
def test_no_bootstrap_configures_versioning_or_object_lock(compose: Path) -> None:
    """Neither profile's one-shots may configure versioning, lock or lifecycle.

    Read as text rather than as parsed YAML on purpose: the bootstraps are
    shell heredocs inside `entrypoint`, so the call would be a line of `sh`,
    not a structured field.
    """
    text = compose.read_text(encoding="utf-8")
    for call in _FORBIDDEN_CLI + _FORBIDDEN_GARAGE_CLI:
        assert call not in text, (
            f"{compose.name} issues `{call}`. Object versioning and Object Lock "
            "are not adopted — see VERSIONING_OBJECTLOCK_EVALUATION.md §0. "
            "Enabling either silently defeats the scheduled hard purge (§1); "
            "its §7 preconditions land first."
        )


def test_create_bucket_never_enables_object_lock() -> None:
    """`CreateBucket` must not carry the flag that turns on lock + versioning.

    `--object-lock-enabled-for-bucket` enables versioning implicitly
    (evaluation M10), so it is the one create-time flag that would adopt both
    without any `put-bucket-versioning` call to notice.
    """
    for compose in (_BASE, _GARAGE):
        text = compose.read_text(encoding="utf-8")
        assert "object-lock-enabled-for-bucket" not in text, (
            f"{compose.name}: create-bucket must not enable Object Lock — it "
            "turns versioning on implicitly (VERSIONING_OBJECTLOCK_EVALUATION.md M10)"
        )


def test_no_service_code_path_issues_these_operations() -> None:
    """The service calls the SDK's storage client, never these operations.

    The SDK's own surface is guarded by `test_forbidden_operations_are_never_issued`
    in `media-sdk-m8`; this is the consumer-side half of the same line.
    """
    offenders: list[str] = []
    for source in _service_sources():
        text = source.read_text(encoding="utf-8")
        for call in _FORBIDDEN_PY:
            if call in text:
                offenders.append(f"{source.relative_to(_REPO)}: {call}")
    assert not offenders, (
        "service code issues object versioning / Object Lock / lifecycle "
        f"operations: {offenders}. See VERSIONING_OBJECTLOCK_EVALUATION.md §0."
    )


def test_no_service_code_path_reads_or_writes_a_version_id() -> None:
    """Deletes stay unversioned, and nothing mints a `versionId` URL.

    Two separate claims of the evaluation rest on this: the hard purge deletes
    by key (§1), and the pre-rewrite `Content-Type` version of `OP-08` is not
    reachable through any URL this service issues (§5.2). `ObjectStat` and
    `ObjectWriteResult` do carry a `version_id` field from the SDK — reading
    one back is fine; passing one to an operation is what is guarded here.
    """
    pattern = re.compile(r"\bversion_id\s*=|\bVersionId\b|\bversionId\b")
    offenders = [
        f"{source.relative_to(_REPO)}:{lineno}"
        for source in _service_sources()
        for lineno, line in enumerate(
            source.read_text(encoding="utf-8").splitlines(), start=1
        )
        if pattern.search(line)
    ]
    assert not offenders, (
        "service code passes a version id to a storage operation: "
        f"{offenders}. Buckets are unversioned by decision "
        "(VERSIONING_OBJECTLOCK_EVALUATION.md §0)."
    )


def test_the_evaluation_ships_next_to_the_stack_it_evaluates() -> None:
    """The recommendation these tests enforce must be readable, not implied."""
    assert _EVALUATION.is_file()
    text = _EVALUATION.read_text(encoding="utf-8")
    # The three load-bearing conclusions, each one the reason a test above
    # exists. Asserted on substance, not on prose that may be reworded.
    assert "T28-versioning-objectlock-eval" in text
    assert "recommendation, not an implementation" in text
    assert "COMPLIANCE" in text and "GOVERNANCE" in text
