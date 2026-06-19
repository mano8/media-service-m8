# media-service-m8

![CI/CD](https://github.com/mano8/media-service-m8/actions/workflows/CI.yaml/badge.svg?branch=main)
[![Codacy Badge](https://app.codacy.com/project/badge/Grade/a7fc6b73325c4b2a8066b04bfaac5c8e)](https://app.codacy.com/gh/mano8/media-service-m8/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)
[![codecov](https://codecov.io/gh/mano8/media-service-m8/graph/badge.svg?token=3ZMKKE05BH)](https://codecov.io/gh/mano8/media-service-m8)
[![Docker Pulls](https://img.shields.io/docker/pulls/tepochtli/media-service-m8)](https://hub.docker.com/r/tepochtli/media-service-m8)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://github.com/mano8/media-service-m8/blob/main/LICENSE)

Media microservice. Handles secure uploads, object
storage, presigned delivery, and lifecycle management of media assets. Built on
[`fastapi-m8`](https://github.com/mano8/fastapi-m8) as an **auth consumer** — it validates tokens
issued by `auth_user_service` from [`fa-auth-m8`](https://github.com/mano8/fa-auth-m8) and never holds a private signing key.

---

## Role in the stack

`media_service` is a `fa-auth-m8` **consumer service**: all CORS, health,
metrics, lifespan, and auth wiring come from `create_app`. It validates access
tokens against the auth service (RS256/JWKS by default, HS256 supported) and
queries the auth service's private introspection endpoint over HTTP for stateful
revocation. It does **not** connect to the auth Redis.

The reference deployment is the hardened Docker Compose stack in
[`docker_compose/hardened_media_m8`](docker_compose/hardened_media_m8) (Traefik,
PostgreSQL, MinIO, media Redis, Prometheus, Grafana). See that directory's
README for stack setup.

## API overview

All routes are mounted under `API_PREFIX` (default `/media`). Domain routers:

### Service metadata & health

Auto-mounted by `fastapi-m8` (≥ 2.0.0) `create_app` — the standard m8 triad:

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/{prefix}/meta` | — | Static, cacheable service identity (`service`/`version`/`api_version`/`contract`) read by clients pre-auth to assert compatibility — satisfies `@fa-m8/astro-media-m8`'s `assertMediaServiceM8Compatibility`. Contract `media-service-m8@0.0`, service-version range `>=0.0.8 <0.1.0`. |
| GET | `/ping` | — | Dependency-free **liveness** → `{"status": "ok"}` (prefix-independent, **not** under `API_PREFIX`). |
| GET | `/{prefix}/health/` | — | Dependency-aware **readiness** (DB / Redis / MinIO). |

Point container **liveness** probes at `/ping` and **readiness** probes at `/{prefix}/health/`.
The `/meta` values come from `Settings` (`SERVICE_VERSION` tracks the package version),
so the service fails closed at boot if its identity is undeclared.

### Uploads — `/{prefix}/v1/uploads` (presigned PUT flow)

| Method | Path | Auth | Rate limit | Purpose |
| --- | --- | --- | --- | --- |
| POST | `/v1/uploads/initiate` | user | 20/min | Create an upload session + presigned PUT URL |
| POST | `/v1/uploads/{session_id}/complete` | user | 20/min | Finalize after the client PUTs to MinIO |
| POST | `/v1/uploads/{session_id}/abort` | user | — | Abort an in-progress session |

Flow: `initiate` returns a presigned `PUT` URL and a session id → client uploads
bytes directly to MinIO → `complete` runs three integrity checks then promotes
the `MediaObject` from `PENDING_UPLOAD` to `UPLOADED`:

1. **Size** — `stat.size` must not exceed `MEDIA_MAX_UPLOAD_SIZE_BYTES` (or the
   per-category override from `MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY`).
2. **Magic-byte MIME** — the object's leading bytes are sniffed with `filetype`;
   the detected type must be compatible with the declared `mime_type` (same major
   type for `image/*`, `video/*`, `audio/*`; exact match otherwise).
3. **SHA-256** — when `sha256` is present in the complete request, the object is
   streamed from storage in bounded chunks (`MEDIA_SHA256_VERIFY_CHUNK_SIZE`) and
   hashed incrementally, so it is never buffered whole in memory; a process-wide
   semaphore (`MEDIA_SHA256_VERIFY_MAX_CONCURRENCY`) caps concurrent verifications.

On any failure the session is marked `ABORTED`, a `MediaObject` with
`status=REJECTED` is persisted for the audit trail, and a
`media_uploads_rejected_total{reason}` counter is incremented.

### Objects — `/{prefix}/v1/objects`

| Method | Path | Auth | Rate limit | Purpose |
| --- | --- | --- | --- | --- |
| GET | `/v1/objects` | user | 120/min | List objects (filtered, cursor-paginated) |
| GET | `/v1/objects/{object_id}` | user | — | Fetch object metadata |
| GET | `/v1/objects/{object_id}/download-url` | user | 60/min | Presigned GET URL for download |
| PATCH | `/v1/objects/{object_id}` | user | — | Update mutable metadata |
| DELETE | `/v1/objects/{object_id}` | user | — | Soft-delete (idempotent) |

`GET /v1/objects` returns, for a regular user, their own objects plus anything
`PUBLIC` and same-tenant `TENANT` objects (superusers see all and may pass
`owner_user_id` / `include_deleted`); `PRIVATE`/`SENSITIVE` objects of other
owners stay hidden — see [Access control](#access-control--visibility). Supported
query parameters:
`category`, `visibility`, `status`, `mime_prefix` (e.g. `image/`),
`created_from`/`created_to`, `q` (filename contains), `sort_by`
(`created_at`|`size_bytes`), `order` (`asc`|`desc`), and `limit` (1–100).
Pagination is keyset/cursor based: the response carries an opaque `next_cursor`;
pass it back as `?cursor=` to fetch the next page. Soft-deleted objects are
excluded unless a superuser passes `include_deleted=true`.

### Share links — `/{prefix}/v1/shares` & `/{prefix}/v1/objects/{id}/shares`

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/v1/objects/{object_id}/shares` | owner | Mint a time-boxed signed share link (**201**) |
| GET | `/v1/objects/{object_id}/shares` | owner | List an object's share links |
| DELETE | `/v1/shares/{token_id}` | owner | Revoke a share link (idempotent, **204**) |
| GET | `/v1/shares/{token}` | **public** | Resolve a signed token → presigned download URL |

Creation, listing and revocation are owner-only (superusers may revoke any
link); resolution is **public** so a holder of the token needs no account. The
token is an HMAC-signed authenticator over the link's row id — signed with a
dedicated, media-service-owned `MEDIA_SHARE_SIGNING_SECRET` (kept independent of
any auth-sdk token secret so the auth layer's key lifecycle never breaks share
links; rotating it invalidates outstanding links). A link resolves only while
it is **not expired**, **not revoked**, and **under
`max_uses`**, and — like the owner-facing download path — only once the object
has passed antivirus scanning (otherwise **409**); each successful resolution
increments the use counter. `expires_in` (seconds) and `max_uses` are optional
on create: the caller picks the lifetime, falling back to
`MEDIA_SHARE_DEFAULT_EXPIRES_SECONDS` (default 7 days) and capped at
`MEDIA_SHARE_MAX_EXPIRES_SECONDS` (default 30 days) — both operator-configurable;
a request above the cap is rejected (**422**). Tokens carry an `ON DELETE CASCADE`
foreign key, so a hard-purged object drops its links automatically.

### Admin — `/{prefix}/v1/admin` (superuser only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/admin/storage/stats` | Aggregate counts/bytes by status and category, plus per-owner usage |
| GET | `/v1/admin/uploads/stale` | List `INITIATED` sessions past `expires_at` |
| POST | `/v1/admin/uploads/purge-stale` | Bulk-expire stale sessions |
| GET | `/v1/admin/quotas/{owner_user_id}` | Usage totals and effective quotas for a scope |
| PUT | `/v1/admin/quotas/{owner_user_id}` | Set per-scope `quota_bytes` / `quota_objects` overrides |
| GET | `/v1/admin/maintenance/orphans` | Reconciliation report — storage/DB orphans, both directions (read-only) |
| POST | `/v1/admin/maintenance/orphans/repair?confirm=true` | Delete **storage-orphans** only; dry-run unless `confirm=true` |
| POST | `/v1/admin/maintenance/purge-expired` | Hard-delete soft-deleted objects past the retention window |
| POST | `/v1/admin/subscriptions` | Register a webhook subscriber (**201**) — URL + signing secret + event filter |
| GET | `/v1/admin/subscriptions` | List webhook subscriptions (signing secrets never returned) |
| DELETE | `/v1/admin/subscriptions/{id}` | Delete a webhook subscription (**204**) |

The guard is applied at the router level via
`dependencies=[Depends(get_current_active_superuser)]`.

### Image variants — `/{prefix}/v1/objects/{id}/variants`

| Method | Path | Auth | Rate limit | Purpose |
| --- | --- | --- | --- | --- |
| POST | `/v1/objects/{id}/variants:generate` | user | 30/min | Create a variant job from named presets (**202**) and enqueue it |
| GET | `/v1/objects/{id}/variants` | user | — | List generated variants |
| GET | `/v1/objects/{id}/variants/jobs/{jid}` | user | — | Variant job progress |
| DELETE | `/v1/objects/{id}/variants/{vid}` | user | — | Delete a variant (row + bytes) |

`:generate` accepts `{ "presets": ["thumb", "web", …] }`. The object must be
`UPLOADED` (**409** otherwise) and a processable image (**422** otherwise);
unknown preset names are **422**. The resolver expands each preset × format into
the `VariantSpec`s carried by the enqueued `generate_variants` job — media-service
never imports `imgtools_m8`.

### Presets — `/{prefix}/v1/presets`

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/presets` | user | Built-in presets merged with the caller's named presets |
| POST | `/v1/presets` | user | Create a user-owned named preset (**201**) |
| PATCH | `/v1/presets/{id}` | user | Replace a preset's recipe |
| DELETE | `/v1/presets/{id}` | user | Delete a user preset |

Built-in defaults (`thumb`/`small`/`medium`/`large`) ship as code constants; a
user row of the same name shadows the built-in at resolve time. Each preset is a
local, imgtools-free recipe: one geometry (`image_size`) rendered into one+
formats (`ext` ∈ `WEBP|JPEG|PNG|GIF|AVIF`, `quality` 1–100).

### Internal (service-to-service) — `/{prefix}/v1/internal`

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/v1/internal/objects/{id}/scan-result` | Apply an antivirus verdict (CLEAN → `READY`, else `QUARANTINED`) |
| POST | `/v1/internal/objects/{id}/variants` | Register a worker-written variant (idempotent) |
| PATCH | `/v1/internal/variant-jobs/{jid}` | Advance a variant job's status/progress |

Every internal route requires `Authorization: Bearer <MEDIA_INTERNAL_SERVICE_TOKEN>`,
compared in constant time (`secrets.compare_digest`); anything missing or
mismatched is **403**. These routes are called only by `media-worker-m8`.

## Events / webhooks (transactional outbox)

At each state change media-service writes an **outbox row in the same DB
transaction** as the change (`core/outbox.record_event`), so a committed change is
never silently un-notified and a rolled-back transaction drops the event too.
Emitted events: `object.ready`, `object.deleted`, `scan.failed`, `variant.ready`.

The service-owned maintenance worker's `deliver_outbox` cron drains `PENDING`
rows and POSTs each as a signed [`OutboxEventPayload`](https://pypi.org/project/media-sdk-m8/)
(`{ event_id, event_type, object_id, payload, created_at }`) to every **active**
subscription whose `event_types` filter matches (an empty filter matches all).
Delivery is **at-least-once** (subscribers dedupe on `event_id`); each POST carries
`X-Signature: sha256=<HMAC-SHA256(body, subscription.secret)>` for verification.
Failures retry with exponential backoff (`OUTBOX_BACKOFF_BASE_SECONDS * 2**(attempts-1)`)
until `OUTBOX_MAX_ATTEMPTS`, after which the event is terminally `FAILED`
(poison-message guard). Delivery is DB-heavy, so by the topology rule it runs in
the service-owned worker — **not** the DB-free `media-worker-m8` — adding no new
image, container, port, or credential surface. Each subscription's signing
`secret` is stored per-row (set at create time), never a global env secret.

Manage subscribers via the superuser `POST/GET/DELETE /v1/admin/subscriptions`
routes above; tunables are the non-secret `OUTBOX_*` settings.

## Antivirus scanning

`complete_upload` leaves a new object `scan_status = PENDING` and enqueues a
`scan_object` job on the media Redis queue. `…/download-url` returns **409** until
the worker reports back via `scan-result`: a CLEAN verdict promotes the object to
`READY` and makes it downloadable; an infected object is purged by the worker and
marked `QUARANTINED`. The callback is idempotent.

## Storage quotas & accounting

Every completed upload credits, and every soft-delete debits, a running
`(owner_user_id, tenant_id)` total in the `storage_usage` table (single source
of accounting truth in [`core/quotas.py`](media_service/core/quotas.py)).
`POST /v1/uploads/initiate` refuses up front when the declared
`expected_size_bytes` would push the owner past their ceiling: **413** over the
byte quota, **409** over the object-count quota. Ceilings resolve to the
per-scope admin override if set, otherwise the `MEDIA_DEFAULT_QUOTA_BYTES` /
`MEDIA_DEFAULT_QUOTA_OBJECTS` defaults (unset = unlimited). Refusals increment
`media_uploads_quota_rejected_total{reason="bytes"|"objects"}`. Both quota
endpoints (and the optional `?tenant_id=`) are superuser-only.

### Category & Dashboard

`/{prefix}/category` (CRUD) and `/{prefix}/dashboard` (user-activity stats) are
inherited consumer-template routers retained for ecosystem parity.

> **Note:** media variants (`db_models/media_variants.py`, `schemas/variants.py`,
> `app/routes/variants.py`) are **reserved stubs** — the model exists but no
> variant routes are wired yet.

## Access control · visibility

Read and download access (`GET /v1/objects/{id}`, `…/download-url`, and what a
listing returns) is governed by each object's `visibility`:

| Visibility | Who may read / download |
| --- | --- |
| `PUBLIC` | Any authenticated user |
| `TENANT` | The owner, superusers, and callers in the **same (non-null) tenant** |
| `PRIVATE` / `SENSITIVE` | The owner and superusers only |

The owner and superusers always have access regardless of visibility. A caller
with no tenant never matches a `TENANT` object. Mutations (`PATCH`/`DELETE`)
remain owner-or-superuser only.

Tenancy is taken from the caller's `tenant_id` claim (surfaced on `UserModel` by
`auth-sdk-m8`, requires `fastapi-m8>=1.6.0`) and stamped onto each object at
upload — never from the request body. Objects created by an untenanted caller
stay `tenant_id IS NULL`, for which `TENANT` resolves as owner/superuser-only.

## Visibility → bucket mapping

| Visibility | Bucket setting |
| --- | --- |
| `PUBLIC` | `MINIO_BUCKET_PUBLIC` (`public-media`) |
| `PRIVATE` | `MINIO_BUCKET_PRIVATE` (`private-media`) |
| `TENANT` | `MINIO_BUCKET_PRIVATE` (`private-media`) |
| `SENSITIVE` | `MINIO_BUCKET_SENSITIVE` (`sensitive-media`) |

Lifecycle storage classes map to `MINIO_BUCKET_TEMP` (`temp-media`) and
`MINIO_BUCKET_ARCHIVE` (`archive-media`).

## Auth modes

Set these to match `auth_user_service` exactly:

- **RS256 / JWKS (default):** `ACCESS_TOKEN_ALGORITHM=RS256` + `JWKS_URI`. No
  shared secret or private key needed.
- **HS256:** `ACCESS_TOKEN_ALGORITHM=HS256` + a shared `ACCESS_SECRET_KEY`.
- **`TOKEN_MODE`:** `stateless` | `hybrid` | `stateful`. In `stateful` mode set
  `INTROSPECTION_URL` + `PRIVATE_API_SECRET` (HTTP revocation checks).
- **Boundary claims:** `auth-sdk-m8 >= 1.0.0` defaults `TOKEN_STRICT_VALIDATION`
  on, so `TOKEN_ISSUER` and `TOKEN_AUDIENCE` are required at boot (or opt out
  with `TOKEN_STRICT_VALIDATION=false` for local dev).
- **Event signing:** `EVENT_SIGNING_ENABLED` defaults on; a strong
  `EVENT_SIGNING_KEY` is required at boot or the process fails closed. It must
  match `auth_user_service` — SSE event-stream payloads (below) are
  HMAC-SHA256 signed and verified with it. Set `EVENT_SIGNING_ENABLED=false` to
  disable signing/verification entirely.
- **Auth event stream (`fastapi-m8 >= 1.5.0`):** when `INTROSPECTION_URL` is set,
  the lifespan starts an `AuthEventStreamClient` that consumes session-revoked /
  user-deleted events from fa-auth's private SSE bridge and evicts the local
  validation cache early. It is a **best-effort cache accelerator** — the JTI
  blacklist behind `INTROSPECTION_URL` stays authoritative and stream loss is
  non-fatal. Tune with `EVENT_STREAM_CONNECT_TIMEOUT` / `EVENT_STREAM_READ_TIMEOUT`.

## Response security headers

Headers are applied by the shared `auth-sdk-m8 >= 1.2.1` layer (wired through
`fastapi-m8 >= 1.5.0`) in three tiers:

- **Always on:** `X-Content-Type-Options`, `X-Frame-Options`.
- **Production gate:** `Referrer-Policy`, `Permissions-Policy`.
- **Express opt-in:** `Strict-Transport-Security` (`HSTS_ENABLED`) and
  `Content-Security-Policy` (`CONTENT_SECURITY_POLICY_ENABLED`), both default off
  and **never emitted on `ENVIRONMENT=local`** even when enabled — so a
  production-configured build run on localhost can't poison the host's HSTS cache.

See [`media_service/.example_env`](media_service/.example_env) for the full,
commented set of settings.

## Media Redis

Media-owned state (rate limits, queues, locks, caches) uses the `MEDIA_REDIS_*`
settings and the `media:*` key namespace — **separate** from the auth Redis. Rate
limiting fails open if the media Redis is unavailable. The same Redis backs the
ARQ job queue (`scan_object`, `generate_variants`) consumed by `media-worker-m8`.

## Worker integration

Media-service is the **producer** for background work run by `media-worker-m8`,
sharing storage + job contracts via `media-sdk-m8`. Two settings wire it up:

- `MEDIA_INTERNAL_SERVICE_TOKEN` — shared bearer token the worker presents on the
  `/v1/internal/*` callbacks (set the **same** value here and on the worker).
- `MEDIA_REDIS_*` — the queue the worker reads jobs from.

## Maintenance worker (lifecycle & retention)

DB-coupled housekeeping runs in a **service-owned arq worker** — the *same*
media-service image launched with a command override (no separate build):

```bash
arq media_service.maintenance_worker.WorkerSettings
```

In the hardened compose stack this is the `media_service_worker` container
(`deploy.replicas: 1` — a single scheduler so arq cron never double-fires; runs
**no** migrations). It owns three jobs, on cron and on-demand via the admin
routes above:

- **hard-purge** (daily) — removes bytes + row for objects soft-deleted longer
  than `MEDIA_RETENTION_PURGE_DAYS` (this is the only true hard-delete; the API
  only soft-deletes). Quota is not re-debited.
- **stale-upload expiry** (hourly) — the scheduled form of
  `/v1/admin/uploads/purge-stale`.
- **orphan reconciliation** (daily, report-only) — storage-keys-without-rows and
  DB-rows-without-bytes; repair is opt-in and deletes storage-orphans only.

Unlike `media-worker-m8` (DB-free, enqueue-driven), this worker has direct DB +
storage access and makes no HTTP callbacks, so it needs **no**
`MEDIA_INTERNAL_SERVICE_TOKEN`. Tunables: `MEDIA_PURGE_BATCH_LIMIT`,
`MEDIA_RECONCILE_GRACE_MINUTES`, `MEDIA_RECONCILE_BATCH_LIMIT`,
`MEDIA_PURGE_CRON_HOUR`, `MEDIA_STALE_CRON_MINUTE`.

## Observability

Prometheus metrics are gated by `METRICS_ENABLED` (zero overhead when off). On
top of the shared `fastapi-m8` metric groups, media registers:

- `media_uploads_initiated_total` (labels: `category`, `visibility`)
- `media_uploads_completed_total` (label: `category`)
- `media_uploads_failed_total`
- `media_bytes_uploaded_total` (label: `category`)
- `media_download_urls_generated_total`

The `/metrics` endpoint is registered under `API_PREFIX` only when enabled.

## Development

```sh
# Install (dev extras)
pip install -r media_service/requirements_dev.txt

# Configure
cp media_service/.example_env media_service/.env   # then fill in values

# Run the test suite (100% line+branch coverage enforced)
pytest

# Lint / format / security
ruff check media_service tests
bandit -r media_service
```

Requirements are split into `requirements_base.txt` (runtime, incl. all DB
drivers), `requirements_prod.txt` (+ gunicorn), and `requirements_dev.txt`
(+ pytest, ruff, bandit). Token slugs use `python-slugify`.

## License

See [LICENSE](LICENSE).
