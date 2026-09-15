"""Live proof that the storage backend exposes ONLY the S3 gateway (S2).

The sibling-container half of invariant S2. The other live module
(`test_storage_invariants_live.py`) speaks HTTP/S3 through the public route and
deliberately leaves this row to `docker inspect` / a sibling container; this
module is that row, made executable.

WHY THIS EXISTS
---------------
`-ip.bind=127.0.0.1` binds master (9333), volume (8080), filer (8888) and
webdav (7333) to the container's own loopback, and the migration measured all
four unreachable from a sibling. That measurement was incomplete:
`-s3.ip.bind=0.0.0.0` also publishes three surfaces that were never probed —

  * 8181   Iceberg REST Catalog server (SeaweedFS 4.x starts it by default)
  * 9101   Lance Namespace server      (likewise)
  * 18333  the S3 component's gRPC port (8333 + 10000), which serves
           ``messaging_pb.SeaweedS3IamCache``

The gRPC one was the serious one: an UNAUTHENTICATED ``PutIdentity`` from a
sibling container minted an identity carrying ``["Admin","Read","Write",
"List"]``, and that credential then created buckets and read/wrote every object
over the ordinary S3 API — a complete escape from the scoped ``media-rw`` grant
invariant S4 exists to enforce, available to every service on the storage
networks. Iceberg and Lance are now switched off at the listener
(``-s3.port.iceberg=0``, ``-s3.port.lance=0``); the gRPC port has no bind flag
of its own, so it is closed with mTLS (`seaweedfs/security.toml`, whose CA key
`storage-tls-init` destroys after signing).

Every assertion here is made from a *sibling container on the storage service's
own Docker network* — the only vantage point that answers "can the thing next
to it reach this?". A positive control on 8333 runs in the same pass, so a
blanket networking failure cannot masquerade as a green result.

Opt-in, like the sibling modules: skipped unless ``STORAGE_LIVE_TEST_DOCKER``
is set to a non-empty value, and requires a working ``docker`` on PATH.

    STORAGE_LIVE_TEST_DOCKER           set to 1 to enable this module
    STORAGE_LIVE_TEST_DOCKER_NETWORK   network to probe from
                                       (default: hardened_media_m8_data_net)
    STORAGE_LIVE_TEST_STORAGE_HOST     service name to probe
                                       (default: storage)

Run standalone with ``pytest tests/live_storage -p no:security_tests_m8`` when
`security-tests-m8`'s own live preflight is not satisfied.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

ENABLED = bool(os.environ.get("STORAGE_LIVE_TEST_DOCKER"))
NETWORK = os.environ.get(
    "STORAGE_LIVE_TEST_DOCKER_NETWORK", "hardened_media_m8_data_net"
)
HOST = os.environ.get("STORAGE_LIVE_TEST_STORAGE_HOST", "storage")

# Pinned exactly like every other image this fleet runs (S14).
PROBE_IMAGE = "busybox:1.37.0"
GRPC_IMAGE = "fullstorydev/grpcurl:v1.9.3"

pytestmark = [
    pytest.mark.skipif(
        not ENABLED,
        reason="STORAGE_LIVE_TEST_DOCKER not set - live admin-surface probe is opt-in",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker not on PATH"),
]

# The admin surfaces -ip.bind=127.0.0.1 is responsible for, plus their gRPC
# siblings (port + 10000), plus the two HTTP servers -s3.ip.bind publishes.
LOOPBACK_ONLY_PORTS = [
    pytest.param(9333, id="master"),
    pytest.param(8080, id="volume"),
    pytest.param(8888, id="filer"),
    pytest.param(7333, id="webdav"),
    pytest.param(19333, id="master-grpc"),
    pytest.param(18080, id="volume-grpc"),
    pytest.param(18888, id="filer-grpc"),
    pytest.param(8181, id="iceberg-rest-catalog"),
    pytest.param(9101, id="lance-namespace"),
]


def _reachable(port: int) -> bool:
    """True when a fresh sibling container can open a TCP connection."""
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", NETWORK, PROBE_IMAGE,
            "nc", "-z", "-w", "3", HOST, str(port),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0


def test_s3_gateway_is_reachable_positive_control() -> None:
    """The S3 gateway must be reachable, or every negative below is vacuous."""
    assert _reachable(8333), (
        f"{HOST}:8333 is not reachable from a sibling on {NETWORK!r}. The "
        "negative results in this module would be meaningless, so this is a "
        "broken probe, not a passing security check."
    )


@pytest.mark.parametrize("port", LOOPBACK_ONLY_PORTS)
def test_non_s3_ports_are_unreachable_from_siblings(port: int) -> None:
    assert not _reachable(port), (
        f"{HOST}:{port} is reachable from a sibling container on {NETWORK!r}. "
        "Only the S3 gateway (8333) may be reachable: the admin components are "
        "bound to the container's own loopback by -ip.bind=127.0.0.1, and the "
        "Iceberg (8181) / Lance (9101) servers are switched off by "
        "-s3.port.iceberg=0 / -s3.port.lance=0."
    )


def test_s3_grpc_port_refuses_unauthenticated_calls() -> None:
    """18333 may listen (mTLS), but must refuse a plaintext gRPC dial.

    `-s3.ip.bind` binds the S3 gRPC port alongside the gateway and offers no
    way to separate them, so the port stays open at the TCP level. What must
    not work is talking to it: its `SeaweedS3IamCache` service mints S3
    identities, and an unauthenticated `PutIdentity` produced a working `Admin`
    credential before `[grpc.s3]` mTLS was enabled.
    """
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", NETWORK, GRPC_IMAGE,
            "-plaintext", "-max-time", "15",
            "-d",
            '{"identity":{"name":"probe-escalation",'
            '"credentials":[{"access_key":"PROBEKEY","secret_key":"PROBESECRET"}],'
            '"actions":["Admin","Read","Write","List"]}}',
            f"{HOST}:18333",
            "messaging_pb.SeaweedS3IamCache/PutIdentity",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    combined = (result.stdout + result.stderr).strip()
    assert result.returncode != 0, (
        "An UNAUTHENTICATED PutIdentity succeeded against "
        f"{HOST}:18333 from a sibling container - any service on this network "
        "can mint itself an Admin S3 credential, defeating invariant S4. "
        "Check that seaweedfs/security.toml is mounted at "
        f"/etc/seaweedfs/security.toml and configures [grpc.s3]. Output: {combined!r}"
    )
