"""Live storage-workflow verification (T23-live-e2e).

Drives the full acceptance list from
`.workspace/plans/media-service-m8/todo/object-storage-backend-migration-*.md`'s
``T23-live-e2e`` step against a *running* hardened-stack instance, entirely
over HTTP/S3 — never importing service code, the same way a real client
would: browser-direct POST upload, antivirus scan gating (both the CLEAN and
the INFECTED/QUARANTINED side, the latter via a real EICAR upload), variant
generation, share links, a visibility move (proved as an actual cross-bucket
copy against storage, not just the metadata field), archive export, orphan
reconcile, and hard-purge.

Opt-in and skipped by default: unlike the rest of this folder's suites (which
run against any bootstrapped stack via `security-tests-m8`'s
`configure_from_env`), this module needs an admin bearer token and direct S3
credentials, so it requires ``STORAGE_LIVE_TEST_ADMIN_PASSWORD`` to be set
before it collects any cases. Point it at a *disposable* stack — this test
creates and destroys real objects across every configured bucket and,
depending on which repair/purge steps run, will actually delete storage
orphans and hard-purge soft-deleted rows it created (T23's own acceptance
criteria: they can't be proven any other way).

Configuration (all via environment variable, all default to the
`hardened_media_m8` reference stack's own documented values)::

    STORAGE_LIVE_TEST_AUTH_BASE          default: http://localhost:9000/user
    STORAGE_LIVE_TEST_MEDIA_BASE         default: http://localhost:9000/media
    STORAGE_LIVE_TEST_STORAGE_PUBLIC_BASE  default: https://storage.localhost
    STORAGE_LIVE_TEST_ADMIN_EMAIL        default: admin@example.com
    STORAGE_LIVE_TEST_ADMIN_PASSWORD     required — no default, gates collection
    STORAGE_LIVE_TEST_S3_ACCESS_KEY      required
    STORAGE_LIVE_TEST_S3_SECRET_KEY      required
    STORAGE_LIVE_TEST_S3_REGION          default: eu-west-1
    STORAGE_LIVE_TEST_BUCKET_PRIVATE     default: private-media
    STORAGE_LIVE_TEST_BUCKET_PUBLIC      default: public-media
    STORAGE_LIVE_TEST_BUCKET_TEMP        default: temp-media

The hard-purge step needs one direct SQL statement (backdating
``deleted_at`` past ``MEDIA_RETENTION_PURGE_DAYS`` — waiting out the real
retention window live is not practical) via
``STORAGE_LIVE_TEST_DB_EXEC_COMMAND``, an argv-style command template
(whitespace-split, no shell involved) with a standalone ``{sql}`` token
substituted for the real statement, e.g.::

    docker compose -f hardened_media_m8/docker-compose.yml exec -T \\
        -e PGPASSWORD=... m8_db psql -U media_svc_user -d media_db -c {sql}

Unset skips only that one assertion (the hard-purge steps), not the whole
module.

This module lives outside `tests/live/` on purpose: `security-tests-m8`'s
pytest plugin (a `pytest11` entry point, so it loads whenever the package is
installed) runs its own live-stack preflight at session start, independent
of which files are collected, and that preflight speaks `LIVE_TEST_*` env
vars this module does not use. If that preflight is not satisfied (no
`tests/live/.env`, or `LIVE_TEST_ADMIN_EMAIL`/`_PASSWORD` unset) it aborts
the *whole* pytest session before collection — run this module standalone
with ``pytest -p no:security_tests_m8`` in that case.
"""

from __future__ import annotations

import io
import os
import shlex
import socket
import subprocess
import time
import uuid
from urllib.parse import urlparse

import boto3
import pytest
import requests
from botocore.config import Config as BotoConfig
from PIL import Image

pytestmark = [pytest.mark.live]

_ADMIN_PASSWORD = os.environ.get("STORAGE_LIVE_TEST_ADMIN_PASSWORD")

pytest.importorskip("boto3")

if not _ADMIN_PASSWORD:
    pytest.skip(
        "STORAGE_LIVE_TEST_ADMIN_PASSWORD not set — this suite mutates real "
        "storage/DB state and only runs against an explicitly configured, "
        "disposable stack.",
        allow_module_level=True,
    )

AUTH_BASE = os.environ.get("STORAGE_LIVE_TEST_AUTH_BASE", "http://localhost:9000/user")
MEDIA_BASE = os.environ.get(
    "STORAGE_LIVE_TEST_MEDIA_BASE", "http://localhost:9000/media"
)
STORAGE_PUBLIC = os.environ.get(
    "STORAGE_LIVE_TEST_STORAGE_PUBLIC_BASE", "https://storage.localhost"
)
ADMIN_EMAIL = os.environ.get("STORAGE_LIVE_TEST_ADMIN_EMAIL", "admin@example.com")
S3_ACCESS_KEY = os.environ["STORAGE_LIVE_TEST_S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["STORAGE_LIVE_TEST_S3_SECRET_KEY"]
S3_REGION = os.environ.get("STORAGE_LIVE_TEST_S3_REGION", "eu-west-1")
BUCKET_PRIVATE = os.environ.get("STORAGE_LIVE_TEST_BUCKET_PRIVATE", "private-media")
BUCKET_PUBLIC = os.environ.get("STORAGE_LIVE_TEST_BUCKET_PUBLIC", "public-media")
BUCKET_TEMP = os.environ.get("STORAGE_LIVE_TEST_BUCKET_TEMP", "temp-media")
DB_EXEC_COMMAND = os.environ.get("STORAGE_LIVE_TEST_DB_EXEC_COMMAND")

# DNS shim: a presigned URL's Host must match the FQDN it was signed for
# (SigV4 GET binds Host; the POST-policy path shares the same public
# endpoint by convention) even on a host with no *.localhost wildcard
# resolution. Scoped to exactly the configured storage host.
_STORAGE_HOST = urlparse(STORAGE_PUBLIC).hostname
if _STORAGE_HOST and _STORAGE_HOST not in ("localhost", "127.0.0.1"):
    _orig_getaddrinfo = socket.getaddrinfo

    def _patched_getaddrinfo(host, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        return _orig_getaddrinfo(
            "127.0.0.1" if host == _STORAGE_HOST else host, *a, **kw
        )

    socket.getaddrinfo = _patched_getaddrinfo

# This stack's auth chain (RS256 verify -> JWKS fetch -> revocation
# introspection) has shown transient 401/403/503s that clear within a couple
# of seconds with no code change and the exact same token/request. Retrying a
# *transient* code that is not one of the caller's own expected outcomes
# absorbs that without masking a real, persistent failure — those still
# exhaust the retries and fail normally.
_TRANSIENT_CODES = (400, 401, 403, 503)

EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _call(
    method: str, url: str, *, expect: tuple[int, ...], retries: int = 20, **kwargs
):
    resp = None
    timeout = kwargs.pop("timeout", 20)
    for _attempt in range(retries):
        resp = requests.request(method, url, timeout=timeout, **kwargs)
        if resp.status_code in expect or resp.status_code not in _TRANSIENT_CODES:
            return resp
        time.sleep(1.5)
    return resp


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login() -> str:
    resp = _call(
        "POST",
        f"{AUTH_BASE}/login/access-token",
        expect=(200,),
        data={"username": ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=STORAGE_PUBLIC,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        verify=False,
    )


def _make_png_bytes(
    size: tuple[int, int] = (32, 32), color: tuple[int, int, int] = (200, 30, 30)
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _browser_direct_upload(
    token: str, *, category: str, visibility: str, mime_type: str, data: bytes
) -> dict:
    """Initiate + browser-direct POST (never through media_service) + complete."""
    init_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/uploads/initiate",
        expect=(200,),
        headers=_auth_headers(token),
        json={
            "category": category,
            "visibility": visibility,
            "original_filename": f"{uuid.uuid4().hex}.png",
            "mime_type": mime_type,
            "expected_size_bytes": len(data),
        },
    )
    assert init_resp.status_code == 200, init_resp.text
    init = init_resp.json()

    files = {"file": (f"{uuid.uuid4().hex}.png", data, mime_type)}
    post_resp = requests.post(
        init["upload_url"],
        data=init["upload_fields"],
        files=files,
        timeout=30,
        verify=False,
    )
    assert post_resp.status_code in (200, 201, 204), (
        f"browser-direct POST failed: status={post_resp.status_code} body={post_resp.text[:300]}"
    )

    complete_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/uploads/{init['session_id']}/complete",
        expect=(200,),
        headers=_auth_headers(token),
        json={},
    )
    assert complete_resp.status_code == 200, complete_resp.text
    return complete_resp.json()["media_object"]


def _wait_for_scan_status(token: str, object_id: str, *, timeout_s: int = 60) -> str:
    deadline = time.time() + timeout_s
    last = "pending"
    while time.time() < deadline:
        resp = _call(
            "GET",
            f"{MEDIA_BASE}/v1/objects/{object_id}",
            expect=(200,),
            headers=_auth_headers(token),
        )
        resp.raise_for_status()
        last = resp.json()["scan_status"]
        if last != "pending":
            return last
        time.sleep(2)
    return last


def _wait_for_status(
    get_status,
    terminal_values: tuple[str, ...],
    *,
    timeout_s: int,
    interval_s: float = 2.0,
) -> str:
    deadline = time.time() + timeout_s
    status = get_status()
    while time.time() < deadline and status not in terminal_values:
        time.sleep(interval_s)
        status = get_status()
    return status


def test_storage_workflow_live() -> None:
    """One sequential run through every T23 acceptance bullet.

    A single test function, not one case per bullet: every later step
    consumes state (an uploaded object, a variant, a share token) created by
    an earlier one, so splitting it would just move the same ordering
    dependency into fixture scope without buying independent failure
    isolation — a failure's step name is already in the assertion message.
    """
    token = _login()
    s3 = _s3_client()

    # ── 1+2. Browser-direct POST upload + scan gating ────────────────────────
    clean_png = _make_png_bytes()
    clean_obj = _browser_direct_upload(
        token,
        category="asset",
        visibility="private",
        mime_type="image/png",
        data=clean_png,
    )
    clean_id = clean_obj["id"]
    assert _wait_for_scan_status(token, clean_id) == "clean"

    dl_resp = _call(
        "GET",
        f"{MEDIA_BASE}/v1/objects/{clean_id}/download-url",
        expect=(200,),
        headers=_auth_headers(token),
    )
    assert dl_resp.status_code == 200, dl_resp.text

    # EICAR as text/plain: unsniffable-but-allowed, so it clears the
    # magic-byte gate at complete_upload and reaches ClamAV for a real
    # verdict — the reject side of scan gating, not just the happy path.
    second_obj = _browser_direct_upload(
        token,
        category="document",
        visibility="private",
        mime_type="text/plain",
        data=EICAR,
    )
    second_id = second_obj["id"]
    second_status = _wait_for_scan_status(token, second_id)
    assert second_status in ("infected", "quarantined"), second_status

    infected_dl = _call(
        "GET",
        f"{MEDIA_BASE}/v1/objects/{second_id}/download-url",
        expect=(409,),
        headers=_auth_headers(token),
    )
    assert infected_dl.status_code == 409, infected_dl.text

    # ── 3. Variant generation ────────────────────────────────────────────────
    presets_resp = _call(
        "GET", f"{MEDIA_BASE}/v1/presets", expect=(200,), headers=_auth_headers(token)
    )
    assert presets_resp.status_code == 200, presets_resp.text
    preset_name = presets_resp.json()[0]["name"]

    gen_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/objects/{clean_id}/variants:generate",
        expect=(202,),
        headers=_auth_headers(token),
        json={"presets": [preset_name]},
    )
    assert gen_resp.status_code == 202, gen_resp.text
    job_id = gen_resp.json()["id"]

    def _variant_job_status() -> str:
        resp = _call(
            "GET",
            f"{MEDIA_BASE}/v1/objects/{clean_id}/variants/jobs/{job_id}",
            expect=(200,),
            headers=_auth_headers(token),
        )
        return resp.json()["status"]

    job_status = _wait_for_status(
        _variant_job_status, ("completed", "failed"), timeout_s=60
    )
    assert job_status == "completed", job_status

    variants_resp = _call(
        "GET",
        f"{MEDIA_BASE}/v1/objects/{clean_id}/variants",
        expect=(200,),
        headers=_auth_headers(token),
    )
    assert variants_resp.status_code == 200 and variants_resp.json()["count"] >= 1, (
        variants_resp.text
    )

    # ── 4. Share links ────────────────────────────────────────────────────────
    share_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/objects/{clean_id}/shares",
        expect=(201,),
        headers=_auth_headers(token),
        json={},
    )
    assert share_resp.status_code == 201, share_resp.text
    share = share_resp.json()

    resolve_resp = _call(
        "GET", f"{MEDIA_BASE}/v1/shares/{share['token']}", expect=(200,)
    )
    assert resolve_resp.status_code == 200, resolve_resp.text
    signed_url = resolve_resp.json()["url"]

    bytes_resp = requests.get(signed_url, timeout=15, verify=False)
    assert bytes_resp.status_code == 200 and bytes_resp.content == clean_png

    revoke_resp = _call(
        "DELETE",
        f"{MEDIA_BASE}/v1/shares/{share['id']}",
        expect=(204,),
        headers=_auth_headers(token),
    )
    assert revoke_resp.status_code == 204, revoke_resp.text
    # A dead link (revoked/expired/exhausted) is answered 403 by design —
    # SharesController.resolve gives no distinct code for "revoked" vs
    # "unknown token".
    resolve_after = _call(
        "GET", f"{MEDIA_BASE}/v1/shares/{share['token']}", expect=(403,)
    )
    assert resolve_after.status_code == 403, resolve_after.status_code

    # ── 5. Visibility move (cross-bucket copy) ───────────────────────────────
    object_key = clean_obj["object_key"]
    assert clean_obj["storage_bucket"] == BUCKET_PRIVATE

    patch_resp = _call(
        "PATCH",
        f"{MEDIA_BASE}/v1/objects/{clean_id}",
        expect=(200,),
        headers=_auth_headers(token),
        json={"visibility": "public"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["storage_bucket"] == BUCKET_PUBLIC

    anon_get = _call("GET", f"{MEDIA_BASE}/v1/objects/{clean_id}", expect=(200,))
    assert anon_get.status_code == 200, anon_get.text

    # Real cross-bucket copy proof, straight from storage.
    s3.head_object(Bucket=BUCKET_PUBLIC, Key=object_key)  # raises if absent
    with pytest.raises(Exception):  # noqa: B017, PT011 — any botocore ClientError means "gone", which is the assertion
        s3.head_object(Bucket=BUCKET_PRIVATE, Key=object_key)

    # ── 6. Archive export ─────────────────────────────────────────────────────
    export_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/export",
        expect=(202,),
        headers=_auth_headers(token),
        json={"format": "archive"},
    )
    assert export_resp.status_code == 202, export_resp.text
    export_job = export_resp.json()

    def _export_status() -> str:
        nonlocal export_job
        resp = _call(
            "GET",
            f"{MEDIA_BASE}/v1/export/{export_job['id']}",
            expect=(200,),
            headers=_auth_headers(token),
        )
        export_job = resp.json()
        return export_job["status"]

    export_status = _wait_for_status(
        _export_status, ("completed", "failed"), timeout_s=90, interval_s=3.0
    )
    assert export_status == "completed", export_status

    archive_bytes_resp = requests.get(
        export_job["download_url"], timeout=30, verify=False
    )
    assert archive_bytes_resp.status_code == 200 and len(archive_bytes_resp.content) > 0

    # ── 7. Orphan reconcile ───────────────────────────────────────────────────
    orphan_key = f"live-test-orphan-{uuid.uuid4().hex}.bin"
    s3.put_object(Bucket=BUCKET_TEMP, Key=orphan_key, Body=b"orphan-bytes")

    # A full bucket-listing sweep across every configured bucket; generous
    # timeout on a long-lived shared stack with real accumulated objects.
    orphans_resp = _call(
        "GET",
        f"{MEDIA_BASE}/v1/admin/maintenance/orphans",
        expect=(200,),
        headers=_auth_headers(token),
        timeout=90,
    )
    assert orphans_resp.status_code == 200, orphans_resp.text
    orphans = orphans_resp.json()
    assert any(
        o["bucket"] == BUCKET_TEMP and o["object_key"] == orphan_key
        for o in orphans["storage_orphans"]
    ), orphans

    repair_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/admin/maintenance/orphans/repair?confirm=true",
        expect=(200,),
        headers=_auth_headers(token),
        timeout=90,
    )
    assert repair_resp.status_code == 200, repair_resp.text
    assert repair_resp.json()["repaired"] >= 1, repair_resp.text

    with pytest.raises(Exception):  # noqa: B017, PT011
        s3.head_object(Bucket=BUCKET_TEMP, Key=orphan_key)

    # ── 8. Hard purge ─────────────────────────────────────────────────────────
    del_resp = _call(
        "DELETE",
        f"{MEDIA_BASE}/v1/objects/{second_id}",
        expect=(204,),
        headers=_auth_headers(token),
    )
    assert del_resp.status_code == 204, del_resp.text

    if not DB_EXEC_COMMAND:
        pytest.skip(
            "STORAGE_LIVE_TEST_DB_EXEC_COMMAND not set — cannot backdate "
            "deleted_at past the retention window, so the purge step cannot "
            "be exercised without waiting out real retention days."
        )

    sql = (
        f"UPDATE app_media_object SET deleted_at = now() - interval '2 days' "
        f"WHERE id = '{second_id}';"
    )
    # No shell=True: DB_EXEC_COMMAND is split into argv tokens and the literal
    # "{sql}" token is replaced with the real statement, so the SQL never
    # passes through a shell's own quoting rules (POSIX sh vs cmd.exe disagree
    # on quote characters, and this must work on both).
    cmd = [sql if token == "{sql}" else token for token in shlex.split(DB_EXEC_COMMAND)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

    purge_resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/admin/maintenance/purge-expired",
        expect=(200,),
        headers=_auth_headers(token),
        timeout=60,
    )
    assert purge_resp.status_code == 200, purge_resp.text
    assert purge_resp.json()["purged"] >= 1, purge_resp.text

    gone_resp = _call(
        "GET",
        f"{MEDIA_BASE}/v1/objects/{second_id}",
        expect=(404,),
        headers=_auth_headers(token),
    )
    assert gone_resp.status_code == 404, gone_resp.status_code

    with pytest.raises(Exception):  # noqa: B017, PT011
        s3.head_object(
            Bucket=second_obj["storage_bucket"], Key=second_obj["object_key"]
        )
