# media-service-m8

Media microservice for the m8 ecosystem. Handles secure uploads, object
storage, presigned delivery, and lifecycle management of media assets. Built on
[`fastapi-m8`](../fastapi-m8) as an **auth consumer** — it validates tokens
issued by `auth_user_service` and never holds a private signing key.

---

## Role in the stack

`media_service` is a `fastapi-m8` **consumer service**: all CORS, health,
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

### Uploads — `/{prefix}/v1/uploads` (presigned PUT flow)

| Method | Path | Auth | Rate limit | Purpose |
| --- | --- | --- | --- | --- |
| POST | `/v1/uploads/initiate` | user | 20/min | Create an upload session + presigned PUT URL |
| POST | `/v1/uploads/{session_id}/complete` | user | 20/min | Finalize after the client PUTs to MinIO |
| POST | `/v1/uploads/{session_id}/abort` | user | — | Abort an in-progress session |

Flow: `initiate` returns a presigned `PUT` URL and a session id → client uploads
bytes directly to MinIO → `complete` verifies the object (MinIO `stat`) and
promotes the `MediaObject` from `PENDING_UPLOAD` to `UPLOADED`.

### Objects — `/{prefix}/v1/objects`

| Method | Path | Auth | Rate limit | Purpose |
| --- | --- | --- | --- | --- |
| GET | `/v1/objects/{object_id}` | user | — | Fetch object metadata |
| GET | `/v1/objects/{object_id}/download-url` | user | 60/min | Presigned GET URL for download |
| PATCH | `/v1/objects/{object_id}` | user | — | Update mutable metadata |
| DELETE | `/v1/objects/{object_id}` | user | — | Soft-delete (idempotent) |

### Admin — `/{prefix}/v1/admin` (superuser only)

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/v1/admin/storage/stats` | Aggregate counts/bytes by status and category |
| GET | `/v1/admin/uploads/stale` | List `INITIATED` sessions past `expires_at` |
| POST | `/v1/admin/uploads/purge-stale` | Bulk-expire stale sessions |

The guard is applied at the router level via
`dependencies=[Depends(get_current_active_superuser)]`.

### Category & Dashboard

`/{prefix}/category` (CRUD) and `/{prefix}/dashboard` (user-activity stats) are
inherited consumer-template routers retained for ecosystem parity.

> **Note:** media variants (`db_models/media_variants.py`, `schemas/variants.py`,
> `app/routes/variants.py`) are **reserved stubs** — the model exists but no
> variant routes are wired yet.

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
  `EVENT_SIGNING_KEY` is required at boot or the process fails closed. This is a
  boot-time requirement only — the auth-state event bus is **not wired into any
  service yet**, so the key is not actively used at runtime. Set
  `EVENT_SIGNING_ENABLED=false` if you prefer to defer it until the bus lands.

See [`media_service/.example_env`](media_service/.example_env) for the full,
commented set of settings.

## Media Redis

Media-owned state (rate limits, queues, locks, caches) uses the `MEDIA_REDIS_*`
settings and the `media:*` key namespace — **separate** from the auth Redis. Rate
limiting fails open if the media Redis is unavailable.

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
