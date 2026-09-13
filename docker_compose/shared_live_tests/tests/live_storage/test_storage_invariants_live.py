"""Live security-invariant checks at the storage boundary (T24-security-regression-matrix).

The wire-level half of the S1–S15 regression matrix
(`docker_compose/hardened_media_m8/SECURITY_REGRESSION_MATRIX.md`): the rows
that can be proven over plain HTTP/S3 against a *running* stack and that a
compatibility table cannot answer. Every assertion here is on a status line,
an S3 error code or a response header read off the wire — never on a
client-side guard — because a silent pass on S9/S10/S11 is precisely the
failure mode the migration plan exists to prevent (`CONTRACT.md`, rule 2).

Rows covered, each as its own test named after the invariant id:

    S3   per-bucket CORS scoped to the UI origin, never `*`
    S4   the app credential is denied outside its five buckets
    S5   an unsigned request is refused on every bucket
    S6   the public route is TLS-only, Host-pinned, and denies the two
         non-S3 liveness paths the S3 port serves
    S9   the app-minted POST policy's `content-length-range` is enforced by
         the server (EntityTooLarge / EntityTooSmall)
    S10  the app-minted POST policy's exact `Content-Type` is enforced by the
         server, and the REPLACE self-copy at `complete` runs
    S11  `response-content-disposition: attachment` is honoured on the app's
         download URL — with a negative control, and with a hostile filename
    S12  a ranged GET returns `206 Partial Content`

S1, S2, S14 and S15 need `docker inspect` / a sibling container and stay in
the matrix document; S7, S8 and S13 are config/static rows owned by unit
suites. The static halves of every row are in `tests/test_compose_*` and the
`media-sdk-m8` conformance harness.

Opt-in exactly like the sibling workflow suite: gated on
``STORAGE_LIVE_TEST_ADMIN_PASSWORD`` and configured by the same
``STORAGE_LIVE_TEST_*`` variables (see `test_storage_workflow_live.py`), plus:

    STORAGE_LIVE_TEST_CORS_ORIGIN         default: https://localhost:4430
                                          (the stack's S3_CORS_ALLOW_ORIGIN)
    STORAGE_LIVE_TEST_STORAGE_HTTP_BASE   default: http://storage.localhost
                                          (the plain-HTTP side of the route,
                                          expected to redirect to https)

Point it at a *disposable* stack: it creates real objects (all removed
again) and one deliberately unlisted bucket name it expects to be denied.
Run standalone with ``pytest tests/live_storage -p no:security_tests_m8``
when `security-tests-m8`'s own live preflight is not satisfied.
"""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import ssl
import time
import uuid
import warnings
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
import requests
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from PIL import Image

pytestmark = [pytest.mark.live]

_ADMIN_PASSWORD = os.environ.get("STORAGE_LIVE_TEST_ADMIN_PASSWORD")

pytest.importorskip("boto3")

if not _ADMIN_PASSWORD:
    pytest.skip(
        "STORAGE_LIVE_TEST_ADMIN_PASSWORD not set — this suite creates real "
        "objects and only runs against an explicitly configured, disposable stack.",
        allow_module_level=True,
    )

AUTH_BASE = os.environ.get("STORAGE_LIVE_TEST_AUTH_BASE", "http://localhost:9000/user")
MEDIA_BASE = os.environ.get(
    "STORAGE_LIVE_TEST_MEDIA_BASE", "http://localhost:9000/media"
)
STORAGE_PUBLIC = os.environ.get(
    "STORAGE_LIVE_TEST_STORAGE_PUBLIC_BASE", "https://storage.localhost"
)
STORAGE_HTTP = os.environ.get(
    "STORAGE_LIVE_TEST_STORAGE_HTTP_BASE", "http://storage.localhost"
)
ADMIN_EMAIL = os.environ.get("STORAGE_LIVE_TEST_ADMIN_EMAIL", "admin@example.com")
S3_ACCESS_KEY = os.environ["STORAGE_LIVE_TEST_S3_ACCESS_KEY"]
S3_SECRET_KEY = os.environ["STORAGE_LIVE_TEST_S3_SECRET_KEY"]
S3_REGION = os.environ.get("STORAGE_LIVE_TEST_S3_REGION", "eu-west-1")
BUCKET_PRIVATE = os.environ.get("STORAGE_LIVE_TEST_BUCKET_PRIVATE", "private-media")
BUCKET_PUBLIC = os.environ.get("STORAGE_LIVE_TEST_BUCKET_PUBLIC", "public-media")
BUCKET_TEMP = os.environ.get("STORAGE_LIVE_TEST_BUCKET_TEMP", "temp-media")
BUCKET_SENSITIVE = os.environ.get(
    "STORAGE_LIVE_TEST_BUCKET_SENSITIVE", "sensitive-media"
)
BUCKET_ARCHIVE = os.environ.get("STORAGE_LIVE_TEST_BUCKET_ARCHIVE", "archive-media")
BUCKETS = (BUCKET_PUBLIC, BUCKET_PRIVATE, BUCKET_SENSITIVE, BUCKET_TEMP, BUCKET_ARCHIVE)
CORS_ORIGIN = os.environ.get("STORAGE_LIVE_TEST_CORS_ORIGIN", "https://localhost:4430")

# Same DNS shim as the workflow suite: the presigned Host must match the FQDN
# it was signed for even where `*.localhost` does not resolve.
_STORAGE_HOST = urlparse(STORAGE_PUBLIC).hostname
if _STORAGE_HOST and _STORAGE_HOST not in ("localhost", "127.0.0.1"):
    _orig_getaddrinfo = socket.getaddrinfo

    def _patched_getaddrinfo(host, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        return _orig_getaddrinfo(
            "127.0.0.1" if host == _STORAGE_HOST else host, *a, **kw
        )

    socket.getaddrinfo = _patched_getaddrinfo

_TRANSIENT_CODES = (400, 401, 403, 503)


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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _s3_error_code(resp: requests.Response) -> str:
    body = resp.text or ""
    if "<Code>" in body:
        return body.split("<Code>", 1)[1].split("</Code>", 1)[0]
    return "(no S3 error body)"


def _png_bytes(size: tuple[int, int] = (32, 32)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _initiate(token: str, *, mime: str, size: int, filename: str | None = None) -> dict:
    resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/uploads/initiate",
        expect=(200,),
        headers=_auth(token),
        json={
            "category": "asset",
            "visibility": "private",
            "original_filename": filename or f"{uuid.uuid4().hex}.png",
            "mime_type": mime,
            "expected_size_bytes": size,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _post_policy(
    init: dict, data: bytes, *, content_type: str | None = None, omit_type: bool = False
) -> requests.Response:
    """Browser-direct POST of the app-minted form, optionally tampered with."""
    fields = dict(init["upload_fields"])
    part_type = fields["Content-Type"]
    if content_type is not None:
        fields["Content-Type"] = part_type = content_type
    if omit_type:
        fields.pop("Content-Type")
    return requests.post(
        init["upload_url"],
        data=fields,
        files={"file": ("f.png", data, part_type)},
        timeout=30,
        verify=False,
    )


def _policy_conditions(init: dict) -> list:
    return json.loads(base64.b64decode(init["upload_fields"]["policy"]))["conditions"]


def _complete(token: str, session_id: str) -> dict:
    resp = _call(
        "POST",
        f"{MEDIA_BASE}/v1/uploads/{session_id}/complete",
        expect=(200,),
        headers=_auth(token),
        json={},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["media_object"]


def _wait_clean(token: str, object_id: str, *, timeout_s: int = 90) -> None:
    deadline = time.time() + timeout_s
    status = "pending"
    while time.time() < deadline and status == "pending":
        resp = _call(
            "GET",
            f"{MEDIA_BASE}/v1/objects/{object_id}",
            expect=(200,),
            headers=_auth(token),
        )
        status = resp.json()["scan_status"]
        if status == "pending":
            time.sleep(2)
    assert status == "clean", status


def _download_url(token: str, object_id: str) -> str:
    resp = _call(
        "GET",
        f"{MEDIA_BASE}/v1/objects/{object_id}/download-url",
        expect=(200,),
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["url"]


def _raw_get(url: str, *, extra_headers: dict[str, str] | None = None):
    """GET over a raw TLS socket so the request line reaches the proxy verbatim.

    `requests` re-quotes some percent-escapes in the path; a browser does not.
    Returns ``(status, headers)``.
    """
    parsed = urlparse(url)
    port = parsed.port or 443
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    lines = [
        f"GET {parsed.path}?{parsed.query} HTTP/1.1",
        f"Host: {parsed.netloc}",
        "Connection: close",
    ]
    lines.extend(f"{k}: {v}" for k, v in (extra_headers or {}).items())
    request = ("\r\n".join(lines) + "\r\n\r\n").encode()
    with (
        socket.create_connection((parsed.hostname, port), timeout=15) as sock,
        ctx.wrap_socket(sock, server_hostname=parsed.hostname) as tls,
    ):
        tls.sendall(request)
        buf = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode(errors="replace").split("\r\n")
    status = int(head[0].split()[1])
    headers = {}
    for line in head[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return status, headers


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def token() -> str:
    resp = _call(
        "POST",
        f"{AUTH_BASE}/login/access-token",
        expect=(200,),
        data={"username": ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def s3():
    return boto3.client(
        "s3",
        endpoint_url=STORAGE_PUBLIC,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
        verify=False,
    )


@pytest.fixture(scope="module")
def clean_object(token: str):
    """One real, scanned-clean PNG uploaded through the app; removed at teardown."""
    data = _png_bytes()
    init = _initiate(token, mime="image/png", size=len(data))
    assert _post_policy(init, data).status_code == 204
    obj = _complete(token, init["session_id"])
    _wait_clean(token, obj["id"])
    yield obj
    requests.delete(
        f"{MEDIA_BASE}/v1/objects/{obj['id']}", headers=_auth(token), timeout=15
    )


# ── S3 ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bucket", BUCKETS)
def test_s3_bucket_cors_is_scoped_to_ui_origin(s3, bucket: str) -> None:
    rules = s3.get_bucket_cors(Bucket=bucket)["CORSRules"]
    origins = [o for rule in rules for o in rule.get("AllowedOrigins", [])]
    assert origins == [CORS_ORIGIN], origins
    assert "*" not in origins


def test_s3_preflight_from_foreign_origin_gets_no_allow_origin() -> None:
    resp = requests.options(
        f"{STORAGE_PUBLIC}/{BUCKET_PRIVATE}/some-key",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
        verify=False,
        timeout=15,
    )
    acao = resp.headers.get("Access-Control-Allow-Origin")
    assert acao in (None, ""), (resp.status_code, acao)


def test_s3_preflight_from_ui_origin_is_allowed() -> None:
    resp = requests.options(
        f"{STORAGE_PUBLIC}/{BUCKET_PRIVATE}/some-key",
        headers={"Origin": CORS_ORIGIN, "Access-Control-Request-Method": "POST"},
        verify=False,
        timeout=15,
    )
    assert resp.status_code == 200, resp.status_code
    assert resp.headers.get("Access-Control-Allow-Origin") == CORS_ORIGIN


# ── S4 ──────────────────────────────────────────────────────────────────────


def test_s4_app_credential_reaches_its_five_buckets(s3) -> None:
    for bucket in BUCKETS:
        s3.head_bucket(Bucket=bucket)  # raises on 403/404


@pytest.mark.parametrize(
    "operation", ["create_bucket", "put_object", "list_objects_v2"]
)
def test_s4_app_credential_denied_outside_its_buckets(s3, operation: str) -> None:
    unlisted = f"t24-unlisted-{uuid.uuid4().hex[:8]}"
    kwargs = {"Bucket": unlisted}
    if operation == "put_object":
        kwargs.update(Key="x", Body=b"x")
    with pytest.raises(ClientError) as excinfo:
        getattr(s3, operation)(**kwargs)
    assert excinfo.value.response["Error"]["Code"] == "AccessDenied", (
        excinfo.value.response["Error"]
    )


# ── S5 ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bucket", BUCKETS)
def test_s5_unsigned_requests_are_denied(s3, bucket: str) -> None:
    key = f"t24/{uuid.uuid4().hex}"
    s3.put_object(Bucket=bucket, Key=key, Body=b"t24", ContentType="text/plain")
    try:
        obj = requests.get(f"{STORAGE_PUBLIC}/{bucket}/{key}", verify=False, timeout=15)
        listing = requests.get(f"{STORAGE_PUBLIC}/{bucket}/", verify=False, timeout=15)
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
    assert obj.status_code == 403, (obj.status_code, obj.text[:200])
    assert _s3_error_code(obj) == "AccessDenied"
    assert listing.status_code == 403, (listing.status_code, listing.text[:200])


def test_s5_unsigned_list_buckets_is_denied() -> None:
    resp = requests.get(f"{STORAGE_PUBLIC}/", verify=False, timeout=15)
    assert resp.status_code == 403, (resp.status_code, resp.text[:200])


# ── S6 ──────────────────────────────────────────────────────────────────────


def test_s6_plain_http_redirects_to_https() -> None:
    resp = requests.get(
        f"{STORAGE_HTTP}/{BUCKET_PRIVATE}/x", allow_redirects=False, timeout=15
    )
    assert resp.status_code in (301, 308), resp.status_code
    assert resp.headers.get("Location", "").startswith("https://"), resp.headers


@pytest.mark.parametrize(
    "host", ["other.localhost", "storage.evil.example", "localhost"]
)
def test_s6_route_is_host_pinned(host: str) -> None:
    parsed = urlparse(STORAGE_PUBLIC)
    resp = requests.get(
        f"https://127.0.0.1:{parsed.port or 443}/{BUCKET_PRIVATE}/x",
        headers={"Host": host},
        verify=False,
        timeout=15,
    )
    # 404 from Traefik itself: no router matched, the backend never saw it.
    assert resp.status_code == 404, resp.status_code
    assert "seaweedfs" not in resp.headers.get("Server", "").lower()


@pytest.mark.parametrize("path", ["/healthz", "/status", "/healthz/", "/status/x"])
def test_s6_liveness_paths_are_denied_at_the_proxy(path: str) -> None:
    resp = requests.get(f"{STORAGE_PUBLIC}{path}", verify=False, timeout=15)
    assert resp.status_code == 404, (resp.status_code, resp.text[:80])
    assert "seaweedfs" not in resp.headers.get("Server", "").lower()


def test_s6_tls_floor_rejects_tls11() -> None:
    parsed = urlparse(STORAGE_PUBLIC)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    try:
        with warnings.catch_warnings():
            # Offering TLS 1.1 is the point of this probe.
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ValueError, AttributeError):
        pytest.skip("this OpenSSL cannot offer TLS 1.1 at all")
    with pytest.raises((ssl.SSLError, OSError)):
        with (
            socket.create_connection(
                (parsed.hostname, parsed.port or 443), timeout=10
            ) as sock,
            ctx.wrap_socket(sock, server_hostname=parsed.hostname),
        ):
            pass


def test_s6_presigned_get_works_through_the_route(
    token: str, clean_object: dict
) -> None:
    resp = requests.get(
        _download_url(token, clean_object["id"]), verify=False, timeout=15
    )
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])


# ── S9 ──────────────────────────────────────────────────────────────────────


def test_s9_content_length_range_is_enforced_by_the_server(token: str, s3) -> None:
    declared = 1000
    init = _initiate(token, mime="image/png", size=declared)
    conditions = _policy_conditions(init)
    clr = next(
        c for c in conditions if isinstance(c, list) and c[0] == "content-length-range"
    )
    assert clr[1] >= 1 and clr[2] == declared, conditions

    oversized = b"\x89PNG\r\n\x1a\n" + os.urandom(2000)  # incompressible, > declared
    resp = _post_policy(init, oversized)
    assert resp.status_code == 400, (resp.status_code, resp.text[:200])
    assert _s3_error_code(resp) == "EntityTooLarge", resp.text[:200]

    resp = _post_policy(init, b"")
    assert resp.status_code == 400, (resp.status_code, resp.text[:200])
    assert _s3_error_code(resp) == "EntityTooSmall", resp.text[:200]

    conforming = _png_bytes((8, 8))
    assert 0 < len(conforming) <= declared
    resp = _post_policy(init, conforming)
    assert resp.status_code == 204, (resp.status_code, resp.text[:200])

    bucket = urlparse(init["upload_url"]).path.strip("/").split("/")[0]
    key = init["upload_fields"]["key"]
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        assert head["ContentLength"] == len(conforming), head["ContentLength"]
    finally:
        s3.delete_object(Bucket=bucket, Key=key)  # session left un-completed on purpose


# ── S10 ─────────────────────────────────────────────────────────────────────


def test_s10_content_type_is_pinned_by_the_server_and_replaced_on_complete(
    token: str, s3
) -> None:
    data = _png_bytes()
    init = _initiate(token, mime="image/png", size=len(data))
    conditions = _policy_conditions(init)
    assert {"Content-Type": "image/png"} in conditions, conditions  # exact eq

    for bad in ("text/html", "image/png; charset=utf-8", "application/octet-stream"):
        resp = _post_policy(init, data, content_type=bad)
        assert resp.status_code == 403, (bad, resp.status_code, resp.text[:200])
    resp = _post_policy(init, data, omit_type=True)
    assert resp.status_code == 403, (resp.status_code, resp.text[:200])

    resp = _post_policy(init, data)
    assert resp.status_code == 204, (resp.status_code, resp.text[:200])

    bucket = urlparse(init["upload_url"]).path.strip("/").split("/")[0]
    key = init["upload_fields"]["key"]
    before = s3.head_object(Bucket=bucket, Key=key)
    time.sleep(1.1)  # Last-Modified has one-second resolution
    obj = _complete(token, init["session_id"])
    try:
        after = s3.head_object(Bucket=obj["storage_bucket"], Key=obj["object_key"])
        # The REPLACE self-copy at complete rewrites metadata in place: same
        # bytes (same ETag), newer Last-Modified, declared type pinned.
        assert after["ETag"] == before["ETag"]
        assert after["LastModified"] > before["LastModified"], (before, after)
        assert after["ContentType"] == "image/png", after["ContentType"]
    finally:
        requests.delete(
            f"{MEDIA_BASE}/v1/objects/{obj['id']}", headers=_auth(token), timeout=15
        )


def test_s10_replace_copy_rewrites_stored_content_type(s3) -> None:
    """The exact SDK operation `complete` issues, on a scratch key, observed
    both on HeadObject and on what a presigned GET then serves."""
    key = f"t24/{uuid.uuid4().hex}"
    s3.put_object(
        Bucket=BUCKET_TEMP, Key=key, Body=b"<html>x</html>", ContentType="text/html"
    )
    try:
        assert s3.head_object(Bucket=BUCKET_TEMP, Key=key)["ContentType"] == "text/html"
        s3.copy_object(
            Bucket=BUCKET_TEMP,
            Key=key,
            CopySource={"Bucket": BUCKET_TEMP, "Key": key},
            MetadataDirective="REPLACE",
            ContentType="text/plain",
        )
        assert (
            s3.head_object(Bucket=BUCKET_TEMP, Key=key)["ContentType"] == "text/plain"
        )
        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": BUCKET_TEMP, "Key": key}, ExpiresIn=120
        )
        served = requests.get(url, verify=False, timeout=15)
        assert served.status_code == 200
        assert served.headers.get("Content-Type", "").startswith("text/plain"), (
            served.headers
        )
    finally:
        s3.delete_object(Bucket=BUCKET_TEMP, Key=key)


# ── S11 ─────────────────────────────────────────────────────────────────────


def test_s11_download_url_is_served_as_attachment(
    token: str, clean_object: dict
) -> None:
    url = _download_url(token, clean_object["id"])
    params = parse_qs(urlparse(url).query)
    rcd = params.get("response-content-disposition", [""])[0]
    assert rcd.startswith("attachment;"), rcd

    resp = requests.get(url, verify=False, timeout=15)
    assert resp.status_code == 200, (resp.status_code, resp.text[:200])
    served = resp.headers.get("Content-Disposition", "")
    assert served.startswith("attachment;") and "filename=" in served, served


def test_s11_negative_control_without_override_is_inline(
    s3, clean_object: dict
) -> None:
    """Proves the header above is the server honouring the parameter, not a default."""
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": clean_object["storage_bucket"],
            "Key": clean_object["object_key"],
        },
        ExpiresIn=120,
    )
    resp = requests.get(url, verify=False, timeout=15)
    assert resp.status_code == 200, resp.status_code
    assert not resp.headers.get("Content-Disposition", "").startswith("attachment"), (
        resp.headers
    )


def test_s11_hostile_filename_cannot_inject_headers(token: str) -> None:
    hostile = 'x".html\r\nX-Injected: 1.png'
    data = _png_bytes()
    init = _initiate(token, mime="image/png", size=len(data), filename=hostile)
    assert _post_policy(init, data).status_code == 204
    obj = _complete(token, init["session_id"])
    try:
        _wait_clean(token, obj["id"])
        status, headers = _raw_get(_download_url(token, obj["id"]))
        assert status == 200, status
        served = headers.get("content-disposition", "")
        assert served.startswith("attachment;"), served
        assert "\r" not in served and "\n" not in served, served
        assert "x-injected" not in headers, headers
    finally:
        requests.delete(
            f"{MEDIA_BASE}/v1/objects/{obj['id']}", headers=_auth(token), timeout=15
        )


# ── S12 ─────────────────────────────────────────────────────────────────────


def test_s12_ranged_get_returns_partial_content(
    token: str, s3, clean_object: dict
) -> None:
    resp = s3.get_object(
        Bucket=clean_object["storage_bucket"],
        Key=clean_object["object_key"],
        Range="bytes=0-511",
    )
    body = resp["Body"].read()
    assert resp["ResponseMetadata"]["HTTPStatusCode"] == 206
    assert resp.get("ContentRange", "").startswith("bytes 0-"), resp.get("ContentRange")
    assert body[:4] == b"\x89PNG" and len(body) <= 512

    presigned = requests.get(
        _download_url(token, clean_object["id"]),
        headers={"Range": "bytes=0-511"},
        verify=False,
        timeout=15,
    )
    assert presigned.status_code == 206, presigned.status_code
    assert len(presigned.content) <= 512
