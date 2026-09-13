# Security regression matrix — object-storage backend migration (S1–S15)

Signed-off walk of the fifteen security invariants the object-storage backend
migration (MinIO → SeaweedFS 4.x, S3-neutral) must preserve, measured against
the **migrated** `hardened_media_m8` stack. This is `T24-security-regression-
matrix` of the migration plan; the invariant ids, wording and severity come
from the executable contract in `media-sdk-m8/tests/conformance/CONTRACT.md`
(`INVARIANT_CASES` in `contract.py`) and are never renumbered.

**Rule:** every row is `blocker`. A single amber blocks the release. A green
tick on S9, S10, S11 or S12 counts only if it was observed from the storage
server's own response — a client-side guard proves nothing (contract rule 2).

## Measured state

| What | Value |
| --- | --- |
| Date | 2026-09-13 |
| Stack | `hardened_media_m8`, booted for real (the same instance `T23-live-e2e` used; Traefik host ports remapped to `18000`/`14430`/`19000` on this host, internal wiring untouched) |
| Storage | `chrislusf/seaweedfs:4.45` (running image == compose pin, image id `sha256:1c4f296dc6e3…`) |
| Proxy | `traefik:v3.7.5`, shipped `traefik.yml` + `dynamic_conf.yml` |
| `media-service-m8` | `4d4ac14` (`feat/object-storage-backend-migration`) |
| `media-sdk-m8` | `cf21365` (conformance harness re-run, 20/20) |
| `media-worker-m8` | `08ccf95` |
| `fa-ui-m8` | `19f2bf2` (its compose-policy mirror, 66/66) |
| `security-tests-m8` | `a4f0c0a` (`release_hygiene`, 33 passed / 1 skipped) |
| Static suites | `media-service-m8`: 169 passed (`test_compose_storage_policy`, `test_compose_image_pins`, `test_compose_traefik_routing`, `test_storage_client`, `test_compose_secrets_policy`); `media-worker-m8/tests/test_config.py`: 37 passed |
| Live walk | 82/82 S-row observations passed; one non-invariant finding (F1) recorded below — resolved on this branch after the walk, see its entry |

Application containers in the measured stack run local-source builds of this
branch (`media-service-m8:t23-local`, `media-worker-m8:t23-local` via the
untracked `docker-compose.override.t23.yml`) because `media-sdk-m8@0.8.0` is
pinned ahead of its PyPI publish — the plan's recorded window. Every storage,
proxy and infrastructure image is the tracked pin.

## The matrix

Verdict legend: ✅ pass · 🟠 amber (blocks) · ❌ fail (blocks).
`Evidence` names the proof: a test id with `file:line`, a captured response, or
a `docker inspect` field. "Live" means observed against the running stack
during this walk; "static" means asserted by a suite that parses the shipped
files; "conformance" means `pytest -m conformance --backend=seaweedfs` in
`media-sdk-m8` (scratch container of the pinned image, re-run for this walk).

| # | Invariant | Verdict | Evidence |
| --- | --- | --- | --- |
| S1 | Storage publishes **no host port** in hardened; loopback-only in dev | ✅ | **Live:** `docker inspect storage` → `HostConfig.PortBindings = {}`, no entry in `NetworkSettings.Ports` carries a host binding. **Static:** `tests/test_compose_storage_policy.py:102` `TestHardenedStorageNoHostPorts::test_hardened_storage_publishes_no_host_ports`; dev stacks `:491` `TestDevStorageLoopbackOnly` (exactly one `127.0.0.1:`-bound S3 port, never `0.0.0.0`); `fa-ui-m8` mirror `docker_compose/compose_policy_tests/test_compose_storage_policy.py:134`. |
| S2 | Admin/console/filer surface unreachable from Traefik and from any sibling on `app_net` | ✅ | **Live:** a fresh `busybox:1.36` on each of the storage container's networks (`hardened_m8_app_net`, `hardened_media_m8_data_net`) ran `nc -z storage <port>`: `9333`, `8080`, `8888`, `7333`, `18080`, `18888`, `19333` all **refused**, `8333` **open** (so the probe itself is not blind). **Static:** `docker-compose.yml:328-330` `-ip=localhost -ip.bind=127.0.0.1 -s3.ip.bind=0.0.0.0`, asserted by `tests/test_compose_storage_policy.py:120` `TestHardenedStorageAdminSurfaceLoopbackOnly` (3 tests); the only Traefik backend URL naming storage is `http://storage:8333` (`traefik/dynamic_conf.yml`). **Conformance:** `test_security_invariants.py:40` `test_storage_admin_surface_unreachable_from_siblings` pass. |
| S3 | CORS scoped to the UI origin, never `*` | ✅ | **Live:** `GetBucketCors` (media-rw credential) on each of the five buckets → `AllowedOrigins=['https://localhost:4430']`, `AllowedMethods=['GET','HEAD','POST']`; an `OPTIONS` preflight through Traefik from `Origin: https://evil.example` → **403**, no `Access-Control-Allow-Origin`; from the configured origin → **200** with `Access-Control-Allow-Origin: https://localhost:4430`. **Static:** `tests/test_compose_storage_policy.py:375` `TestHardenedStorageCorsBootstrap::test_storage_cors_is_scoped_to_ui_origin` (also asserts the bootstrap script refuses a wildcard); dev stacks `:311`; `fa-ui-m8` mirror `:355` (×2 env files). |
| S4 | App holds a **scoped** credential (5 buckets, Get/Put/Delete/List); root creds only in the one-shot init | ✅ | **Live:** media-rw `HeadBucket` → 200 on all five buckets; `CreateBucket`, `PutObject` and `ListObjectsV2` on `t24-unlisted-bucket` → **403 `AccessDenied`** each; identity table (`/etc/seaweedfs/config/s3.json` inside the container) holds exactly `admin` and `media-rw`, media-rw actions = `Read/Write/List` × exactly the five buckets; `S3_ROOT_USER`/`S3_ROOT_PASSWORD` absent by name **and** by value from `media_service`'s and `media_worker`'s environment (values compared in memory, never printed). **Conformance:** `test_security_invariants.py:69` `test_scoped_credential_denies_unlisted_bucket` pass. Verb mapping (no distinct delete verb — `Write` covers it, bucket scope unchanged) is documented in `README.md` § Object storage and `MATRIX.md` D2. |
| S5 | Data path is presigned-only; no anonymous access | ✅ | **Live:** unsigned `GET /{bucket}/{existing key}` and unsigned `GET /{bucket}/` (list) through Traefik → **403 `AccessDenied`** on every one of the five buckets; unsigned `GET /` (ListBuckets) → 403. **Conformance:** `test_security_invariants.py:125` `test_unsigned_object_request_is_denied` pass. No bucket policy / anonymous grant exists (`FORBIDDEN_OPERATIONS`, `test_forbidden_operations_are_never_issued`). |
| S6 | Public storage route is TLS-only, Host-pinned, `passHostHeader: true` | ✅ | **Live:** plain HTTP `:80` with `Host: storage.localhost` → **301** to `https://…`; HTTPS with `Host: other.localhost` / `storage.evil.example` / `localhost` → **404** from Traefik (no `Server` header, never reaches storage); `/healthz`, `/status`, `/healthz/`, `/status/x` through the route → **404** Traefik; TLS handshake capped at TLS 1.1 → `TLSV1_ALERT_PROTOCOL_VERSION`, at TLS 1.2 → OK; positive control: the app-minted presigned GET through the route → **200** `Server: SeaweedFS 30GB 4.45`. **Static:** `tests/test_compose_storage_policy.py:165` `TestHardenedTraefikStorageRouter` (10 tests: websecure entrypoint, `tls: {}`, `Host()` rule, liveness-path exclusion `:205`, `passHostHeader` `:233`, backend URL, and the roll-up `:248` `test_public_storage_route_is_tls_and_host_pinned`). **Caveat, recorded by `T17` and re-proved here:** `passHostHeader` cannot be proved load-bearing live on this backend — SeaweedFS 4.45 also validates SigV4 against `X-Forwarded-Host`, which Traefik always sets. Replaying the same presigned URL directly at `storage:8333` (bypassing Traefik): with no forwarded host → **403**; with `X-Forwarded-Host: storage.localhost:14430` → **200**; with `X-Forwarded-Host: evil.example` → **403**. Host binding is real; the static assertion is the one that guards the flag. |
| S7 | `*_PUBLIC_ENDPOINT` is `https://` in hardened, loopback in dev; validated at config load | ✅ | **Live:** `media_service` container env `S3_PUBLIC_ENDPOINT=https://storage.localhost:14430`. **Static:** `media_service/core/config.py:347-383` `_validate_s3_public_endpoint` (bare host rejected, non-http(s) rejected, `http://` to a non-loopback host rejected under production/strict), proved by `tests/test_storage_client.py:82-155` (12 tests, incl. `:112` `test_s3_public_endpoint_http_non_loopback_production_rejected`); env examples by `tests/test_compose_storage_policy.py:419` `TestStoragePublicEndpointEnvExample` (8 tests: hardened `https://`, all three dev stacks loopback); `media.env.production.example` → `https://storage.example.com`. Note: the measured instance runs `ENVIRONMENT=local` (a local verification boot), so the production-mode rejection is proved by the unit tests, not by this boot. |
| S8 | Storage secret is in `secret_fields`; worker rejects internal-token == storage-secret | ✅ | **Static:** `media_service/core/config.py:85-93` lists `S3_SECRET_KEY` (and the deprecated `MINIO_SECRET_KEY` until 3.0.0) in `secret_fields` — `tests/test_storage_client.py:294` `test_s3_secret_key_is_a_secret_field`; `media-worker-m8/worker/config.py:217-232` `_assert_token_not_reused` — `media-worker-m8/tests/test_config.py:110` `test_credential_isolation_token_not_s3_key` (and `_not_redis_password`); production env examples omit every secret field — `tests/test_compose_secrets_policy.py:396-420`. **Live:** in both `media_service` and `media_worker`, `MEDIA_INTERNAL_SERVICE_TOKEN ≠ S3_SECRET_KEY ≠ MEDIA_REDIS_PASSWORD` (compared in memory); `GET /media/health` and `/media/metrics` bodies contain no secret value. |
| S9 | **Server-side** `content-length-range` upload cap | ✅ | **Live, app-minted policy, through Traefik:** `POST /media/v1/uploads/initiate` with `expected_size_bytes=1000`; the decoded `policy` field carries `["content-length-range", 1, 1000]` (`{"Content-Type": eq}` and `{"key": eq}` alongside). Browser-direct POST of a **2008-byte** body → **`400 EntityTooLarge`**; a **0-byte** body → **`400 EntityTooSmall`**; a 77-byte body → **204**; `HeadObject` on the staged key afterwards → `ContentLength=77` (the oversized body never landed). **Conformance:** `test_security_invariants.py:156` `test_post_policy_oversized_body_rejected_by_server` pass. Call sites: `media_service/storage/presign.py:33-55`, `media_sdk_m8/storage/client.py:579-621` (`min(expected_size_bytes, category cap)` is the signed maximum — `controllers/uploads.py:268-278`). |
| S10 | **Server-side** exact `Content-Type` pinning + `REPLACE` rewrite | ✅ | **Live, app-minted policy:** condition is `{"Content-Type": "image/png"}` (exact `eq`, not `starts-with`). POST with the field changed to `text/html`, `image/png; charset=utf-8`, `application/octet-stream`, or **omitted** → **`403 AccessDenied`** each; with the pinned value → **204**. REPLACE: after `…/complete`, `HeadObject` shows `Last-Modified` advanced (`10:11:09Z` → `10:11:12Z`) with the same ETag and `Content-Type: image/png` — the self-copy at `controllers/uploads.py:385-398` ran against the live backend; and on a scratch `temp-media` key, `CopyObject` same-key `MetadataDirective=REPLACE` rewrote a stored `text/html` to `text/plain`, which a presigned GET then served. **Conformance:** `test_security_invariants.py:214` `test_post_policy_content_type_mismatch_rejected_by_server` and `test_s3_surface.py` OP-08 pass. |
| S11 | `response-content-disposition: attachment` honoured (anti stored-XSS) | ✅ | **Live, through Traefik:** the app's `GET /media/v1/objects/{id}/download-url` carries `response-content-disposition=attachment; filename="…"; filename*=UTF-8''…`; fetching it returns **200** with the server's `Content-Disposition: attachment; filename="t24-….png"; filename*=UTF-8''t24-….png`. **Negative control:** a presigned GET on the same key minted *without* the override → 200 with **no** `Content-Disposition` — the header above is SeaweedFS honouring the parameter, not a default. **Injection:** `original_filename = x".html\r\nX-Injected: 1.png` → served `Content-Disposition: attachment; filename="x_.html__X-Injected: 1.png"; filename*=UTF-8''x%22.html%0D%0AX-Injected%3A%201.png` — one line, quote and CR/LF neutralised, no injected header (`presign.py:14-29` `_safe_content_disposition`; request sent over a raw TLS socket so the request line reached the proxy verbatim). **Conformance:** `test_security_invariants.py:267` `test_presigned_get_honours_response_content_disposition` pass; version floor `≥ 4.01` asserted by `test_contract_spec.py:236`. |
| S12 | Ranged GET works (magic-byte sniffing gate) | ✅ | **Live:** `GetObject Range: bytes=0-511` (media-rw, through Traefik) → **206**, `Content-Range: bytes 0-115/116`, body starts `\x89PNG`; the app-minted presigned GET with the same `Range` → **206**, `Accept-Ranges: bytes`; inside the `media_service` container, `ObjectStorage.get_object_head` is confirmed to send `Range=f"bytes=0-{length - 1}"`. **Conformance:** `test_security_invariants.py:305` `test_ranged_get_returns_partial_content` and OP-03 pass. |
| S13 | Runtime data dir name blocked from release surfaces | ✅ | **Static:** `security-tests-m8/security_tests_m8/release_hygiene.py:45` `_BLOCKED_RUNTIME_DIR_NAMES = {minio, seaweedfs, redis, media_redis, db_data, vault}` — `tests/test_release_hygiene.py:148` `test_minio_dir_blocked`, `:155` `test_seaweedfs_dir_blocked`. **Live:** `scan_release_surface(hardened_media_m8/)` on this worktree (real `seaweedfs/data` on disk after the boot) → `ERROR runtime-data-dir seaweedfs - runtime data directory must not appear on a release or build surface`; `.gitignore` covers `seaweedfs/data/*` and the generated `seaweedfs/config/s3.json`; `git ls-files seaweedfs` → nothing tracked. |
| S14 | Every image pinned, never `:latest` | ✅ | **Live:** running `storage` image `chrislusf/seaweedfs:4.45` == compose pin; none of the stack's 15 containers runs `:latest` or a bare tag. **Static:** `tests/test_compose_image_pins.py:52` `TestHardenedImagePins` (`storage` → `chrislusf/seaweedfs:4.45`, `storage-init` → `amazon/aws-cli:2.36.40`, `storage-config` → `alpine:3.21.3`), `:99` `TestDevImagePins`; `fa-ui-m8` mirror `test_compose_image_pins.py:92`/`:125`. |
| S15 | Storage container hardened like every other service (**gap closed by T15**) | ✅ | **Live:** `docker inspect storage` → `SecurityOpt=["no-new-privileges:true"]`, `CapDrop=["ALL"]`, `CapAdd=null`, `ReadonlyRootfs=true`, `Memory=1073741824`, `NanoCpus=2000000000`, `User=1000:1000`, `Tmpfs={"/run","/tmp"}`, `Privileged=false`; `docker exec storage` → uid/gid `1000`, `touch /etc/…` → `Read-only file system`; peer `media_service` carries the same keys. **Static:** `tests/test_compose_storage_policy.py:279` `TestHardenedStorageServiceHardening::test_storage_service_carries_standard_hardening`; `fa-ui-m8` mirror `:302`. |

**Sign-off: 15/15 green. No amber. The migrated backend preserves every
invariant the MinIO baseline enforced, and S15 — not enforced on MinIO — is
now enforced too.**

## Findings outside the matrix

### F1 — download URLs 400 at the proxy for filenames containing `;` `%` `?` `#`

Not an S-row and not a regression of this migration — recorded because the
walk found it and it affects the same data path.

* **Observed (live):** uploads named `t24;v2.png`, `t24 100%.png`,
  `t24 what?.png`, `t24 #1.png` each **upload fine** (POST 204 — the key is a
  form field, not in the request path) and **complete fine**, but their
  app-minted download URL returns **`400 Bad Request` from Traefik** (no
  `Server` header, request never reaches storage). The identical URL replayed
  directly at `storage:8333` returns **200**. `t24 plain (1).png`, `my photo.png`
  and `été.png` are unaffected (200 through the route).
* **Cause:** `media_service/storage/keys.py::_safe_filename` embeds the
  original filename verbatim (path-stripped only) in the object key, so the
  presigned GET path carries `%3B` / `%25` / `%3F` / `%23`, and
  `traefik/traefik.yml`'s `encodedCharacters` hardening
  (`allowEncodedSemicolon/Percent/QuestionMark/Hash: false`, added 2026-06-12
  in `bbc94c7`, on `main` before this plan) rejects them. Direct probe of the
  route: `/private-media/k%3B`, `k%25`, `k%2F`, `k%3F`, `k%23`, `k%00` → 400;
  `k%22`, `k%20x`, `k%3A`, `k%0D%0A`, `k%C3%A9` → routed.
* **Security impact:** none — fail-closed (the object is simply not
  retrievable through the public route) and the route's hardening is
  behaving as configured. **Availability impact:** real — every download /
  share link for such a file is dead in the hardened stack, on MinIO as much
  as on SeaweedFS. `T23`'s live suite did not hit it because it names uploads
  with hex UUIDs.
* **Recommended follow-on (not this plan):** sanitise the key segment in
  `keys.py::_safe_filename` (drop or replace `;%?#` and control characters —
  the served filename comes from `original_filename`, not from the key, so
  nothing user-visible changes) and add a live case that uploads such a name
  and fetches it through the route.
* **Resolved 2026-09-13 (same branch, after this walk):** two layers, both
  re-measured against this stack. (1) The key sink: `keys.py::_key_segment`
  maps `;` `%` `?` `#` and C0/DEL controls to `_` before a name enters an
  object key; `tests/test_storage_keys.py` parses every stack's
  `encodedCharacters` block so the rule cannot drift from the proxy config.
  (2) The trust boundary: `core/validation.py`'s portable-filename policy —
  `POST /uploads/initiate` and `PATCH /objects/{id}` refuse (`422`) path
  separators, `< > : " | ? *`, control/format code points (CR/LF, NUL, bidi
  overrides), empty/all-dots and > 255 characters; the archive import
  normalises onto the same rules. Live, on the wire, through the real route:
  `t24;v2.png` / `t24 100%.png` / `t24 #1.png` → **200** with the original
  name in `Content-Disposition` (was 400); `../t24.png`, `t24 what?.png`,
  `t24<U+202E>gnp.exe`, a CR/LF name, `<script>.png` → **422** at initiate,
  no URL minted. `test_storage_invariants_live.py` carries both cases
  (`test_f1_*`, 8 parametrised rows) and now runs **42 passed**; the S11
  hostile-name probe was re-pointed at the sharpest name the policy still
  admits (`x'.html; X-Injected=1 100%.png`) and still proves the header sink
  on its own. Keys already stored under such names are not rewritten —
  `DATA_MIGRATION_RUNBOOK.md` step 1.4 counts them before a migration.

## How to re-run

* Static rows: `pytest tests/test_compose_storage_policy.py
  tests/test_compose_image_pins.py tests/test_compose_traefik_routing.py
  tests/test_storage_client.py tests/test_compose_secrets_policy.py` in
  `media-service-m8`; `pytest tests/test_config.py` in `media-worker-m8`;
  `pytest tests/test_release_hygiene.py` in `security-tests-m8`; the
  `docker_compose/compose_policy_tests` mirror in `fa-ui-m8`.
* Conformance rows: `pytest -m conformance --backend=seaweedfs --no-cov` in
  `media-sdk-m8` (needs Docker).
* Live wire-level rows (S3, S4, S5, S6, S9, S10, S11, S12) against a running
  stack: `docker_compose/shared_live_tests/tests/live_storage/
  test_storage_invariants_live.py` — opt-in via the same
  `STORAGE_LIVE_TEST_*` variables as `T23`'s workflow suite. Every assertion
  there is on a status line or response header read off the wire.
* Container rows (S1, S2, S14, S15): `docker inspect storage` and a
  `busybox` sibling `nc -z` probe as described in the table.
