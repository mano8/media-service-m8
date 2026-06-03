<!-- markdownlint-disable MD024 -->
# Changelog

All notable changes to `media-service-m8` are documented here.

---

## [Unreleased] — Phase 9: Custom Prometheus metrics + fastapi-m8 1.1.0 migration

### Changed

- **`create_app` migrated to `HealthConfig`/`AppLifecycle` API** (`media_service/main.py`).
  The flat kwargs `auth_deps=`, `db_engine=`, `health_checks=` are replaced by two structured
  objects. Requires `fastapi-m8>=1.1.0`.

- **`settings_customise_sources` no-op removed** (`media_service/core/config.py`).
  The key was passed in `SettingsConfigDict` which ignores unknown keys (hence the
  `# type: ignore[typeddict-unknown-key]` suppressor). Vault injection is handled by
  `CommonSettings.settings_customise_sources` classmethod via normal inheritance — the
  explicit key was silently redundant.

### Added

- **`media_service/metrics.py`** — five media-specific Prometheus counters registered
  against the shared `auth_sdk_m8` `REGISTRY`:
  - `media_uploads_initiated_total` (labels: `category`, `visibility`)
  - `media_uploads_completed_total` (label: `category`)
  - `media_uploads_failed_total`
  - `media_bytes_uploaded_total` (label: `category`)
  - `media_download_urls_generated_total`
  All counters are `None` when `METRICS_ENABLED=false` — `inc_*` helpers are always
  callable and simply no-op in that case.
- **`media_service/main.py`** — calls `_media_metrics.setup()` immediately after the
  shared `_metrics.setup()` so counters are registered at startup.
- **`controllers/uploads.py`** — `inc_upload_initiated` after `initiate_upload` commit,
  `inc_upload_completed` after `complete_upload` commit, `inc_upload_failed` after
  `abort_upload` commit.
- **`controllers/objects.py`** — `inc_download_url_generated` after `create_download_url`.
- **Tests** — `tests/test_metrics.py` (9 unit tests using `monkeypatch` to inject mock
  counters; covers both None-guard branches and actual `.inc()` / `.labels().inc()` paths).
  Total: 147 tests, 100% coverage maintained.

---

## [0.8.0] — Phase 8: Admin routes

### Added

- **`schemas/admin.py`** — `StorageStatsResponse`, `StaleUploadsResponse`, `PurgeStaleResponse`
  and their nested types (`StorageStatsByStatus`, `StorageStatsByCategory`, `StaleUploadSession`).
- **`controllers/admin.py`** — `AdminController` with three static methods:
  - `get_storage_stats`: GROUP BY status and category aggregations using
    `func.count` + `func.coalesce(func.sum, 0)`.
  - `get_stale_uploads`: SELECT sessions with `status=INITIATED` and `expires_at < now`.
  - `purge_stale_uploads`: bulk UPDATE to `EXPIRED` + returns `rowcount`.
- **`app/routes/admin.py`** — three superuser-gated endpoints under `/v1/admin`:
  - `GET /v1/admin/storage/stats`
  - `GET /v1/admin/uploads/stale`
  - `POST /v1/admin/uploads/purge-stale`
  Guard applied at router level via `dependencies=[Depends(get_current_active_superuser)]`.
- **Tests** — `tests/test_admin.py` (13 tests: empty DB, non-empty, multi-status,
  active-session exclusion, purge idempotency, 403 guards).
  Total: 138 tests, 100% coverage maintained.

---

## [0.6.0] — Phase 6: Redis rate limiting

### Added

- **`core/rate_limit.py`** — `RateLimiter` callable dependency and `get_redis_client` factory.
  Uses a fixed-window counter (INCR + EXPIRE) keyed as `media:ratelimit:{action}:{user_id}`.
  Fails open on Redis errors so a cache outage never blocks uploads.
- **Per-endpoint limits** applied via `dependencies=[Depends(...)]` on route decorators:
  - `POST /v1/uploads/initiate` — 20 req/min per user
  - `POST /v1/uploads/{id}/complete` — 20 req/min per user
  - `GET /v1/objects/{id}/download-url` — 60 req/min per user
- **`anyio_backend` fixture** in `conftest.py` restricts async test parametrization to
  `asyncio` only (trio is not installed in this environment).
- **Tests** — `tests/test_rate_limit.py` (10 unit tests covering all `RateLimiter` branches),
  plus 429 integration tests in `test_uploads.py` and `test_objects.py`.
  Total: 125 tests, 100% coverage maintained.

---

## [0.5.0] — Phase 5: Upload API, Object API, and full test suite

### Added

- **Upload API** (`/v1/uploads`): presigned PUT flow with `initiate`, `complete`, and `abort` endpoints backed by `UploadsController`.
- **Object API** (`/v1/objects`): `get`, `download-url` (presigned GET), `update` (PATCH), and soft-delete endpoints backed by `ObjectsController`.
- **Schemas** — `schemas/uploads.py` (`UploadInitiateRequest/Response`, `UploadCompleteRequest/Response`) and `schemas/objects.py` (`MediaObjectUpdate`, `DownloadUrlResponse`).
- **Full test suite** — 117 tests, 100% line/branch coverage enforced via `pytest-cov --cov-fail-under=100`.
  - `tests/conftest.py`: SQLite in-memory fixtures, `mock_storage`, `current_user`, `superuser`, `client`, `superuser_client`.
  - `tests/test_uploads.py`, `tests/test_objects.py`: end-to-end route + controller tests including edge cases (expired session, ownership 403, MinIO stat failure, tz-aware datetime, soft-delete idempotency).
  - `tests/test_category.py`, `tests/test_controllers_dashboard.py`, `tests/test_dashboard.py`: route and controller coverage including exception-handler branches.
  - `tests/test_storage_keys.py`, `tests/test_storage_buckets.py`, `tests/test_storage_presign.py`, `tests/test_storage_client.py`: storage layer unit tests.
  - `tests/test_policies.py`, `tests/test_core_db_models.py`, `tests/test_media_redis.py`, `tests/test_core_deps.py`: infrastructure unit tests. (Note: `tests/test_revocation.py` was removed — revocation is delegated to `fastapi-m8`'s `RemoteRevocationClient`.)
- **`.coveragerc`**: excludes `fastapi_pre_start.py` (startup script) from coverage measurement.
- **`python-slugify 8.0.4`** dependency replacing the Python-2-only `slugify 0.0.1`.

### Fixed

- `storage/buckets.py`: removed duplicate `MediaVisibility` enum; now imports from `db_models/media_objects.py` and adds `TENANT` mapping to `private-media`.
- `db_models/media_objects.py`: `MediaObject.status` default changed from `UPLOADED` to `PENDING_UPLOAD`.
- `core/deps.py`, `main.py`: extracted boolean helpers (`_need_revocation_client`, `is_stateful_consumer`) to keep pragma-annotated conditional lines within the 88-character limit.

### Changed

- `app/main.py`: registered `/v1/uploads` and `/v1/objects` routers.
- `README.md`: added full API overview (endpoints, presigned upload flow, visibility/bucket mapping, auth modes, dev commands).
