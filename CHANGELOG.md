<!-- markdownlint-disable MD024 -->
# Changelog

All notable changes to `media-service-m8` are documented here.

---

## [Unreleased]

### Changed

- **Bundled issuer migrated to the per-consumer `1.0.0` image + live-test harness
  alignment (security-tests-m8 ≥ 0.2.0).**
  - `dev_media_m8` and `hardened_media_m8` now pin `tepochtli/fa-auth-m8:1.0.0`
    (was `0.9.9`); `worspace_dev_media_m8` / `dev_local_media_m8` build the issuer
    from source (already `1.0.0`). Every stack now runs a **per-consumer** issuer
    with no legacy single-secret fallback.
  - `PRIVATE_API_CONSUMERS` is now **active** (uncommented) in all four
    `auth.env.example` files — the `1.0.0` issuer fails closed without it, so the
    `media-service` consumer (`INTERNAL_CLIENT_ID=media-service`, already set)
    must be registered. `test_consumer_auth_config.py` flips from asserting the
    registry is commented to asserting it is active and registers `media-service`.
  - All stack `test.env` / `test.env.example` + `shared_live_tests/env.example`
    gain `LIVE_TEST_PRIVATE_API_CLIENT_ID=media-service` (`X-Internal-Client`;
    enables the harness F06 legacy-detection check) and a documented opt-in
    `LIVE_TEST_HEALTH_DETAIL_CREDENTIAL` (deep `/health` detail via the dedicated
    credential decoupled from `PRIVATE_API_SECRET`). `shared_live_tests` README
    env table aligned.

### Added

- **`dev_local_media_m8` local compose stack.** A source-built dev stack (auth +
  media + worker + MinIO/Redis/Postgres/observability) that runs the per-consumer
  `1.0.0` issuer from source, with `PRIVATE_API_CONSUMERS` active and the
  live-test harness env (`LIVE_TEST_PRIVATE_API_CLIENT_ID=media-service`,
  opt-in `LIVE_TEST_HEALTH_DETAIL_CREDENTIAL`) wired like the other stacks.
- **README per-consumer / health-detail consumer-auth documentation.** The root
  `README.md` documents per-consumer internal auth (`INTERNAL_CLIENT_ID` +
  `PRIVATE_API_CONSUMERS`, bootstrap vs. service-token), the revocation
  failure-mode (`ACCESS_REVOCATION_FAILURE_MODE`), and the dedicated
  `HEALTH_DETAIL_CREDENTIAL` health gate (decoupled from `PRIVATE_API_SECRET`).

- **Production overlay with `_FILE` secret mounts (plan 6.1).** Added
  `docker_compose/hardened_media_m8/docker-compose.production.yml` — a thin
  production overlay applied on top of the base stack via
  `docker compose -f docker-compose.yml -f docker-compose.production.yml up -d`.
  Secrets are sourced from operator-managed `./secrets/<name>.txt` files mounted
  at `/run/secrets/` inside each container; the corresponding `*_FILE` env vars
  (e.g. `DB_PASSWORD_FILE`, `MEDIA_INTERNAL_SERVICE_TOKEN_FILE`,
  `MEDIA_SHARE_SIGNING_SECRET_FILE`, `MEDIA_REDIS_PASSWORD_FILE`,
  `MINIO_ACCESS_KEY_FILE`, `MINIO_SECRET_KEY_FILE`, and auth/event-signing
  secrets) are injected by the overlay so values never appear in `docker inspect`.
  Source-code bind mounts are removed from `media_service` / `media_service_worker`
  (pinned published images are used as-is). Production env example files
  (`auth.env.production.example`, `media.env.production.example`,
  `worker.env.production.example`) carry `ENVIRONMENT=production` +
  `STRICT_PRODUCTION_MODE=true` and omit all plaintext secret fields.
  `traefik/production_dynamic_conf.yml` replaces `localhost` host rules with FQDN
  placeholders and raises the TLS floor to 1.3. `tests/test_compose_secrets_policy.py`
  (47 tests) asserts all five `_FILE` categories from plan 6.1, the Docker
  `secrets:` block, no `changethis` in the overlay YAML, and that production env
  examples omit plaintext secrets. 665 tests, 100% cov, ruff + mypy green.

- **`MINIO_PUBLIC_ENDPOINT` setting** — optional full URL (e.g.
  `http://127.0.0.1:9005` or `https://storage.example.com`) that, when set,
  makes presigned upload and download URLs target the browser-reachable MinIO
  endpoint instead of the internal `MINIO_HOST:MINIO_PORT`. All other ops
  (stat, copy, verify, health) continue to use the internal endpoint. Empty
  string (default) preserves existing behaviour.

- **Per-consumer internal auth (9.1 — fastapi-m8 ≥ 3.1.0).** `INTERNAL_CLIENT_ID`
  is now documented and set in all three compose stacks (`dev_media_m8`,
  `hardened_media_m8`, `worspace_dev_media_m8`). When set to `media-service`,
  `build_internal_auth` (fastapi-m8 3.1.0) switches the revocation/introspection
  private call from the legacy single `X-Internal-Token` to per-consumer
  `X-Internal-Client` + `X-Internal-Token` bootstrap mode. `PRIVATE_API_SECRET`
  then carries this consumer's per-consumer bootstrap secret (matched against its
  entry in the issuer's `PRIVATE_API_CONSUMERS` registry). The optional
  `SERVICE_TOKEN_EXCHANGE_ENABLED` flag (commented out — opt-in only) enables
  short-TTL Bearer service-token exchange. `PRIVATE_API_CONSUMERS` is documented
  in all three `auth.env.example` files to show the issuer-side configuration.
  `tests/test_consumer_auth_config.py` audits the env examples and verifies the
  legacy/bootstrap header selection via `build_internal_auth`. 609 tests, 100% cov.

### Changed

- **Bump `fastapi-m8` floor to `>=3.1.0,<4.0.0`** (from `>=3.0.0,<4.0.0`; the
  `3.0.0` floor was set in the SDK-v2 alignment pass on this branch).
  `fastapi-m8` 3.1.0 adds `build_internal_auth` + per-consumer `INTERNAL_CLIENT_ID`
  / `SERVICE_TOKEN_EXCHANGE_ENABLED` settings on `ConsumerServiceSettings`; it
  consumes `auth-sdk-m8>=2.0.1,<3.0.0`. `constraints.txt` / `constraints-all.txt`
  updated to pin `fastapi-m8==3.1.0` and `auth-sdk-m8==2.1.0`.
- **Prior SDK-v2 alignment (on this branch — `fastapi-m8` 3.0.0 / `auth-sdk-m8` 2.0.1).**
  `fastapi-m8` 3.0.0 consumes `auth-sdk-m8>=2.0.1,<3.0.0`; this carried SDK 2.x
  transitively. (Merged into the 3.1.0 bump above.)
- **Activate `tenant_id` in the upload object-key path.**  `initiate_upload` now
  passes `tenant_id` from the authenticated principal to `build_object_key`, so
  tenanted uploads are stored under `tenants/{tenant_id}/users/{owner}/...` rather
  than the flat `users/{owner}/...` path.  Non-tenanted callers (no `tenant_id`
  claim) are unaffected — they continue to use the flat path.  This completes the
  `TENANT`-visibility activation started in the 0.0.9 security pass and ensures
  `build_variant_key` (which already receives `media_object.tenant_id`) produces
  a consistent key when the worker processes the original object.
- **`/ping` route is single-mount at `{prefix}/ping` (SDK 2.0.0).** `auth-sdk-m8`
  2.0.0 dropped the dual-mount pattern (bare root `/ping` + prefixed `/ping`);
  with a configured prefix the route is registered exactly once at `/media/ping`.
  Tests updated accordingly: `test_ping_route_prefix_independent` is replaced by
  `test_ping_route_not_at_bare_root` (asserts 404), and
  `test_ping_schema_carries_single_operation` now asserts `/media/ping` is in the
  schema and `/ping` is absent.

- **Service version → `0.0.9`** (`media_service.__version__`, from `0.0.8`). The
  `GET {prefix}/meta` contract id stays `media-service-m8@0.0` (the whole pre-1.0
  line shares contract `0.0`); its service-version `range` tracks the bump to
  `>=0.0.9 <0.1.0` (`CONTRACT_RANGE` in `core/config.py`). The hardened compose
  stack pins the matching published image `tepochtli/media-service-m8:0.0.9`
  (alongside `tepochtli/fa-auth-m8:0.9.9` and `tepochtli/media-worker-m8:0.2.0`).
- **`/health` + OpenAPI now report the package version.** `create_app` in
  `main.py` was passing a hard-coded `service_version="1.0.0"`; it now passes
  `settings.SERVICE_VERSION`, so the readiness body and the OpenAPI `info.version`
  agree with `GET {prefix}/meta` (`0.0.9`) instead of reporting a stale `1.0.0`.
- **Bundled `fa-auth-m8` image bumped `0.9.8` → `0.9.9`** in the `dev_media_m8`
  and `hardened_media_m8` compose stacks (`worspace_dev_media_m8` builds
  `fa-auth-m8` from source and is unaffected).
- **`docker_compose` documentation realigned with the stacks.** All compose
  READMEs were rewritten to match the actual services and pins: stale per-stack
  titles fixed (`dev_media_m8` / `worspace_dev_media_m8` were copies of the
  hardened README), the previously-undocumented `clamav` / `media_worker` /
  `media_service_worker` services added, MinIO image tags + the `quay.io/minio/mc`
  registry corrected, and the hardened README no longer advertises the MinIO host
  ports removed in security item 0.2. The top-level `docker_compose/README.md`
  (a leftover `fa-auth-m8` template listing stacks that do not exist here) now
  describes the three real media stacks. The `worspace_dev_media_m8` local
  cross-repo dev stack is now tracked (config only — `*.env`, generated keys, and
  runtime volume data stay git-ignored).
- **Standalone `media_service/.example_env` completed.** Added the two required
  secrets it was missing (`MEDIA_INTERNAL_SERVICE_TOKEN`,
  `MEDIA_SHARE_SIGNING_SECRET`) and corrected `MEDIA_REDIS_USER` from the
  retired `appuser` default to the scoped `media` user, so the non-Docker local
  example boots without the fail-closed settings error.

### Fixed

- **`/ping` schema assertion is FastAPI 0.137+ compatible.** `test_meta.py` read
  `app.routes` to assert the prefixed `/media/ping` copy stays out of the schema;
  FastAPI 0.137 stopped flattening included routers onto `app.routes` (they become
  nested entries with `path=None`), so the walk found nothing. The test now asserts
  the schema contract through the public `app.openapi()["paths"]` document instead.

- **Pin `media-sdk-m8>=0.4.0`** (from `>=0.3.0`) — the streaming SHA-256
  verification (6.x.3) calls the SDK's new chunked `ObjectStorage.stream_object`
  primitive, which first ships in 0.4.0, so the floor is now a hard requirement,
  not just an alignment. The `media-sdk-m8` pin in `constraints.txt` /
  `constraints-all.txt` is moved to `==0.4.0` to keep the lockfiles consistent
  with the floor; both should be regenerated via `pip-compile` against the
  published 0.4.0 release as part of the final remediation PR.

### Security

- **P0.3 Variant preset/job fan-out bounded by cost (service-side).** An
  authenticated caller could multiply CPU, memory, queue time, and storage writes
  with oversized presets or large/duplicate variant jobs — the request path had no
  cost ceilings. media-service is now the request-policy owner with fixed,
  single-validated-path bounds: per preset, each fixed dimension/`fixed_size` ≤
  **8192 px**, `fixed_width × fixed_height` ≤ **32 MP**, `max_byte_size` ≤
  **25 MiB**, and ≤ **5** distinct formats (`PresetSpec` enforces these on every
  construction path — user create/update *and* loading a stored row or a built-in
  default). Per `:generate` request, ≤ **16** preset names (raw, then
  order-preserving de-duplication before resolution), the expansion may not exceed
  **32** outputs, and the summed per-output pixel-area cost (unspecified dimensions
  charged at the per-side max) may not exceed **256 MP** — any overrun is a
  deterministic **422** before a `VariantJob` is created or enqueued. Ceilings are
  fixed code constants (not per-deployment tunables), so no user-facing policy is
  duplicated into clients; `media-worker-m8` keeps its own independent runtime
  ceilings as defense in depth. New `tests/test_variant_cost_bounds.py` covers the
  per-preset, per-request, and per-job bounds plus the `_geometry_cost` upper-bound
  helper; `tests/test_variants_generate.py` adds end-to-end dedupe and
  too-many-presets cases. README variant + preset sections updated. 745 tests,
  100% cov, ruff + mypy + bandit green.

- **P0.2 Upload quota enforced against actual stored size.** Upload quota could
  be bypassed by under-declaring `expected_size_bytes`: initiate checked the
  *declared* size and completion never re-checked the actual object against the
  quota. Now `UploadInitiateRequest` requires `expected_size_bytes >= 1` and
  within the category maximum (**422** otherwise), and the presigned POST policy
  is signed for the **declared** size, not the category maximum, so a small
  declaration cannot smuggle a large object through the signed form. Completion
  rejects when the actual `stat.size` exceeds the declared/category ceiling, and
  a new `quotas.reserve_storage_for_object` takes a **row lock** (`FOR UPDATE`)
  on the owner's `storage_usage` row to enforce the byte/object quota against the
  *actual* stored size in the same transaction that promotes the object —
  closing the under-declare bypass and serialising concurrent completions so they
  cannot both overrun the ceiling. Over-quota completions are rejected (**422**,
  staged bytes removed, `media_uploads_quota_rejected_total` incremented) and
  never credited. `tests/test_uploads.py` / `tests/test_quotas.py` cover zero /
  negative / over-category declarations, under-declared completion, actual-size
  over-quota at completion, tenant scoping, and a serialised concurrent-completion
  no-overrun case. README storage-quota section updated. 725 tests, 100% cov,
  ruff + mypy + bandit green.

- **P0.1 Variant generation scan-readiness gate.** `:generate`
  (`VariantsController.generate`) now requires the source object to have cleared
  antivirus scanning **and** reached its ready lifecycle state
  (`scan_status == CLEAN` and `status == READY`) before a `VariantJob` is created
  or enqueued — previously it accepted any `UPLOADED` object regardless of scan
  outcome, so unscanned/quarantined/infected bytes could be handed to the image
  worker for decoding. All pre-ready, pending, failed, quarantined, infected, or
  mismatched states now return a uniform **409** (matching the download/share scan
  gates) before any ARQ enqueue; soft-deleted sources remain **404**.
  `tests/test_variants_generate.py` is reworked to assert the `READY/CLEAN` happy
  path enqueues exactly one job and that every rejected state fails with no job
  row and no enqueue. README variant section updated.

- **9.3 Decouple the deep-`/health` detail gate from `PRIVATE_API_SECRET`.** The
  `/{prefix}/health/` detail body is now gated by a dedicated, separately-rotatable
  `HEALTH_DETAIL_CREDENTIAL` (+`_FILE`) instead of reusing the private-API secret —
  **fail-closed** when unset (shallow status only, no detail body). A startup
  assertion makes reuse of `PRIVATE_API_SECRET` as either `HEALTH_DETAIL_CREDENTIAL`
  or `METRICS_SCRAPE_CREDENTIAL` a fatal `ConfigurationError`. `HEALTH_DETAIL_CREDENTIAL`
  is documented (commented-out, fail-closed by default) in all three stack
  `media.env.example` files and in `media.env.production.example` (with its `_FILE`
  sourcing note). `tests/test_health_guard.py` gains 5 tests: fail-closed when unset;
  `PRIVATE_API_SECRET` no longer opens detail; both reuse variants fatal; distinct
  credentials accepted. Mirrors the fastapi-m8 / fa-auth-m8 decoupling so no
  operational surface reuses the private-API secret. 699 tests, 100% cov, ruff +
  mypy + bandit green.

- **9.2 Compose `SECURITY.md` — inter-service trust model + mTLS guidance.** Added
  `docker_compose/SECURITY.md`: an inter-service trust-model table
  (`media_service` → auth private API, `media_worker` → `media_service` internal
  callback), a network-segmentation diagram, and single-host vs. multi-host mTLS
  guidance cross-referencing the canonical auth-sdk-m8 `SECURITY.md` section. The
  app-layer per-consumer credential check is stated as the **primary** control,
  mTLS as defense-in-depth. `docker_compose/README.md` and the root README link to
  it. 694 tests, 100% cov, ruff + mypy + bandit green.

- **5.5 Consumer-side revocation-503 degradation matrix.**
  `ACCESS_REVOCATION_FAILURE_MODE` is documented in all three `media.env.example`
  files (`fail_closed` commented-out in the dev stacks; explicit `fail_closed` in
  `hardened_media_m8`). When introspection is unavailable, `fail_closed` returns
  **503** end-to-end through `get_current_user`; `fail_open` accepts the token and
  logs the conscious opt-out (`security.revocation_fail_open`). New
  `tests/test_revocation_degradation.py` (9 tests) covers the matrix, verifies
  `fail_closed` is the default, and audits all three env examples for the setting.
  auth-sdk-m8 2.0.1 / fastapi-m8 3.0.0 minimum. 618 tests, 100% cov, ruff + mypy +
  bandit green.

- **5.4 `API_BIND_IP` static compose-policy tests.** New
  `tests/test_compose_api_bind_ip.py` (29 tests): all three dev stacks assert
  `${API_BIND_IP:-127.0.0.1}:9000` (never `0.0.0.0:9000`); the production overlay
  is verified to drop `:9000` entirely (`traefik ports: !override` → `:80`/`:443`
  only); 18 env examples are scanned for `API_BIND_IP=0.0.0.0`; the merge-tag
  (`!reset`/`!override`) loader is exercised. Mirrors the fa-auth-m8 static suite.
  694 tests, 100% cov, ruff + mypy + bandit green.

- **6.x.1 Per-service scoped Redis ACLs.** Both compose stacks (`dev_media_m8`,
  `hardened_media_m8`) replaced the open `appuser ~* +@all` ACL on **both** Redis
  services with scoped per-service users. `redis_cache` (the bundled auth
  service's Redis) now creates a scoped `auth` user restricted to the auth key
  prefixes (`oauth_session:`/`auth_code:`/`login:`/`refresh:`/`exchange:`/`rt:`/
  `jwt:blacklist:`/`rate:`/`api_key:`), mirroring fa-auth-m8. `media_redis_cache`
  (the media-owned Redis) creates a scoped `media` user restricted to the
  `media:*` namespace plus the `arq:*` queue keys. Both grant only the command
  categories the apps use (`+@read +@write +@transaction +@connection +eval
  -@dangerous +client|setinfo`); the `media` user additionally re-grants `+info`
  **after** `-@dangerous` (ACL rules apply left-to-right) because ARQ issues
  `INFO server` on startup to read the Redis version — without it the worker dies
  with `NoPermissionError running 'info'`. The `default` user is locked to
  `resetkeys -@all +@connection -@dangerous` (healthcheck `PING` only). Env
  examples wire `REDIS_USER=auth` / `MEDIA_REDIS_USER=media` (auth.env, media.env,
  worker.env), and the `MEDIA_REDIS_USER` settings default moved `appuser`→`media`.
  Locked by `tests/test_compose_redis_acl_policy.py` (no open ACL, scoped key
  patterns, category allow/deny, default-user lockdown, env wiring, + source-linked
  guards re-deriving the media namespace default and ARQ usage). 100% coverage,
  ruff + mypy + bandit green.

- **4.3 Dependency constraint files for reproducible builds.** `constraints.txt`
  and `constraints-all.txt` generated via `pip-compile` (pip-tools) from
  `requirements_base.txt` and `requirements_dev.txt` respectively, pinning every
  transitive dependency to a specific version. CI already runs `pip-audit`,
  `bandit`, and Trivy fs+image scans; the constraint files add a reproducible
  build surface so deployable images can be assembled from a fully pinned
  dependency set. Locked by `tests/test_dependency_constraints.py` (6 tests):
  both files exist, carry the pip-compile autogeneration header, and pin every
  direct dependency (excluding pip-compile "unsafe" packages `pip`/`setuptools`
  which are intentionally omitted). 523 tests, 100% coverage, ruff + mypy +
  bandit green.

- **4.1 Pin all bare/unversioned image references.** Both `dev_media_m8` and
  `hardened_media_m8` compose stacks previously referenced three images without
  any version tag: `alpine` (cert-init), `quay.io/minio/minio`, and `minio/mc`
  (untagged = pulls whatever `latest` is at pull time, non-reproducible and
  unauditable). All three are now pinned to explicit version tags:
  `alpine:3.21.3`, `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772`,
  `quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z` (switched from Docker Hub
  `minio/mc` to `quay.io/minio/mc` for registry consistency with the server
  image). Static policy tests in `tests/test_compose_image_pins.py` (13 tests)
  assert both stacks: no bare image names, no `:latest` tag, and the three
  previously-bare images resolve to the expected pinned prefixes.

- **6.x.5 Outbound webhook SSRF controls.** Webhook subscriber URLs are
  operator-supplied, so the transactional-outbox delivery path could otherwise be
  steered at the cloud-metadata endpoint, a loopback admin port, or an internal
  service. A new guard (`core/ssrf.py`) validates a target by **resolving its host
  at send time and inspecting every resolved IP** — DNS rebinding included — and is
  applied at two points: a static pass at subscription create time (scheme, the
  production HTTPS rule, and literal-IP targets; a loopback/metadata literal is now
  rejected with `400`) and the authoritative pass before every delivery POST
  (`OutboxDeliveryController` threads a `url_guard`; a blocked target is never
  requested and settles via the existing retry/backoff path). Honouring the
  home-lab rule, the posture degrades gracefully: loopback, link-local, the
  `169.254.169.254` cloud-metadata address, multicast and reserved ranges are
  **always** blocked, while private (RFC1918/ULA/CGNAT) targets and plain `http://`
  are allowed in local/dev but **rejected under production/strict** — so a
  Docker-network subscriber still works in dev without weakening production. A
  trusted in-cluster subscriber can be exempted by exact hostname via the new
  `MEDIA_WEBHOOK_ALLOWED_INTERNAL_HOSTS` setting (documented in the dev + hardened
  `media.env.example`). New `tests/test_ssrf.py` plus create-time and send-time
  cases in `test_subscriptions.py` / `test_outbox.py`. Full suite 510 tests, 100%
  coverage, ruff + mypy + bandit green.

- **6.x.4 Atomic share `max_uses` consumption.** Resolving a share link no
  longer reads `uses`, checks it against `max_uses`, and writes back in separate
  steps — a window in which two concurrent resolves of a `max_uses`-bounded link
  could both pass the check and both increment, overshooting the limit. A use is
  now consumed by a single conditional `UPDATE ... SET uses = uses + 1 WHERE
  id = ? AND NOT revoked AND expires_at > now AND (max_uses IS NULL OR uses <
  max_uses)` (`SharesController._consume_use`). The database evaluates the
  predicate and the increment as one statement, so concurrent resolves serialise
  there: exactly one caller wins the last use and any loser — having passed the
  read-time check — gets a uniform `403`. The read-time check in
  `_load_active_share` is retained purely to return a precise reason for an
  already-dead link. New tests cover the single-winner guarantee, unlimited
  links, and revoked/expired rejection at the DB layer. Full suite 468 tests,
  100% coverage, ruff + mypy + bandit green.

- **Proxy-routable media liveness probe.** Bumped the `fastapi-m8` floor to
  `>=2.1.0`, which guarantees `auth-sdk-m8 >= 1.5.0` and the shared
  dual-mounted ping route. Media-service now serves both root `GET /ping` for
  direct container probes and `GET /media/ping` for Traefik routes that only
  forward `PathPrefix(/media)`. The prefixed copy is hidden from OpenAPI, so the
  schema still carries a single `ping` operation.

- **6.x.3 Streaming SHA-256 upload verification.** The optional integrity check
  on `complete` no longer downloads the whole object into memory
  (`get_object` → `verify_sha256(content, expected)`). It now streams the object
  from storage in bounded chunks via the new SDK primitive
  `ObjectStorage.stream_object(...)` and hashes incrementally
  (`verify_sha256_stream`), so a large (size-capped) upload is never buffered
  whole. Hard max-size enforcement still runs **before** hashing (step 1), so
  only size-validated objects are read. A process-wide
  `sha256_verification_guard()` (bounded semaphore) caps how many verifications
  run at once, preventing a burst of completions from fanning out into
  unbounded concurrent full-object reads. Two new settings (both defaulted,
  backward-compatible): `MEDIA_SHA256_VERIFY_CHUNK_SIZE` (default 1 MiB) and
  `MEDIA_SHA256_VERIFY_MAX_CONCURRENCY` (default 4); documented in both
  `dev_media_m8` / `hardened_media_m8` `media.env.example`. New tests cover
  streamed valid/invalid/empty hashes, a `tracemalloc`-asserted no-full-buffer
  guarantee, and the concurrency cap. Full suite 461 tests, 100% coverage,
  ruff + mypy + bandit green.

- **6.x.2 Rate-limiter Redis-error failure mode** (`MEDIA_RATE_LIMIT_FAILURE_MODE`).
  Adds an explicit, observable policy for what the rate limiter does when its
  Redis backend is unreachable.
  - `fail_open` (default, backward-compatible): traffic passes through; a Redis
    outage never blocks media uploads, but limits are temporarily unenforced.
  - `fail_closed`: returns HTTP 503 on Redis error; prevents unenforced upload
    bursts during outages. **Recommended for production.**
  Both modes emit a `{api_prefix}_media_rate_limit_redis_errors_total{mode=…}`
  Prometheus counter on every Redis error so outages and policy decisions are
  observable. `hardened_media_m8/media.env.example` sets `fail_closed`;
  `dev_media_m8/media.env.example` documents the option (commented out,
  defaults to `fail_open`).
  7 new tests (both modes, metric emission, settings-read-at-call-time, no-metric
  on success); full suite 457 tests, 100% coverage, ruff + mypy + bandit green.

- **0.4 Advisory deployment preflight wired into init** (P0 consolidation).
  `docker_compose/shared/scripts/init-common.sh` now shells out to the
  `security-tests-m8 preflight` Python scanner after copying env files. The
  invocation is **advisory only** — a non-zero exit is captured, reported, and
  ignored; `compose up` is never blocked. When `security-tests-m8` is not
  installed a clear install note is printed instead. The scanner (updated in
  `security-tests-m8`) now covers two new P0 generic gates that apply to this
  stack: Docker socket mounts and `0.0.0.0` public-bind for any service in
  hardened/production stacks. MinIO-specific compose-policy tests (stronger
  "no `ports:` at all" assertion) are already owned by this repo
  (`tests/test_compose_minio_policy.py`) from item 0.2.

- **0.2 MinIO host-port exposure removed** (P0 stop-the-bleed).
  `hardened_media_m8`: MinIO `ports:` block removed entirely — the API
  (`:9000`) and console (`:9001`) are reachable only on the Docker network
  (`minio:9000`), never from the host or LAN.
  `dev_media_m8`: ports bound to loopback (`127.0.0.1:9005:9000`,
  `127.0.0.1:9006:9001`) for local tooling; no LAN exposure.
  Static compose-policy tests added (`tests/test_compose_minio_policy.py`,
  5 tests) asserting the above for both stacks.

---

## [0.0.8] — 2026-06-16 · Service `/meta` + `/ping` routes (contract discoverability)

Closes item 6 of `dev-stack-runtime-errors.md`: the service exposed no
service/contract version metadata for clients to assert compatibility.

> **Requires `fastapi-m8 >= 2.0.0`** (which auto-mounts the routes from
> `ConsumerServiceSettings`, in turn requiring `auth-sdk-m8 >= 1.4.0`).

### Added

- **`GET /media/meta`** — static, cacheable service identity
  (`service`/`version`/`api_version`/`contract`) read by clients pre-auth;
  satisfies `@fa-m8/astro-media-m8`'s `assertMediaServiceM8Compatibility`.
  Contract `media-service-m8@0.0` (pre-1.0, tracks the package major.minor),
  service-version range `>=0.0.8 <0.1.0` (0.0.8 is the first release exposing
  the route).
- **`GET /ping`** — prefix-independent, dependency-free liveness (`{"status": "ok"}`),
  kept separate from the dependency-aware `/media/health/` readiness probe.

### Changed

- `fastapi-m8` dependency bumped to `>=2.0.0,<3.0.0`; the now-required
  `SERVICE_VERSION` / `CONTRACT_VERSION` / `CONTRACT_RANGE` (+ `CONTRACT_NAME`)
  settings are declared in `.env`.

---

## [0.0.7] — 2026-06-15 · Phase 16 · events / webhooks (transactional outbox)

Reliable, at-least-once event delivery to subscriber URLs. Each state change
writes an outbox row **in the same DB transaction** as the change, so no committed
state change is ever silently un-notified; the service-owned maintenance worker
drains and POSTs them, HMAC-signed, with retry/backoff and a poison-message cap.

### Added

- **`db_models/outbox.py`** — `OutboxEvent` (`event_type`, `object_id`, JSON
  `payload`, `status` `PENDING|DELIVERED|FAILED`, `attempts`, `next_attempt_at`,
  `created_at`, `delivered_at`) and `Subscription` (`url`, per-row signing
  `secret`, `event_types` filter, `active`).
- **`core/outbox.py`** — `record_event(session, ...)` stages an outbox row on the
  caller's session **without committing**, so it flushes atomically with the
  state change (and rolls back with it). Named `outbox` (not `events`) to avoid
  colliding with `core/events.py`, the unrelated inbound auth event-stream.
- **Transactional emit at every state change** — `object.ready` /
  `scan.failed` (`ObjectsController.apply_scan_result`), `object.deleted`
  (`delete_object`, guarded by the existing idempotency check), and
  `variant.ready` (`VariantsController.register_variant`).
- **`controllers/outbox.py`** — sync `OutboxDeliveryController.deliver_pending`:
  claims due `PENDING` rows (batch-bounded), POSTs each as a signed
  `OutboxEventPayload` to every active matching subscription (empty `event_types`
  = all), settles `DELIVERED` else increments `attempts` with exponential backoff
  (`base * 2**(attempts-1)`) until `OUTBOX_MAX_ATTEMPTS` marks it terminally
  `FAILED`. The `X-Signature` header is `sha256=<HMAC-SHA256(body, secret)>`.
- **`deliver_outbox` cron** added to `maintenance_worker.WorkerSettings`
  (functions + cron). DB-heavy, so by the topology rule it lands in the
  service-owned worker — **no new image, container, port, or credential surface**;
  `media-worker-m8` stays DB-free. Runs once per minute (latency-sensitive).
- **Subscription admin routes** (superuser): `POST /v1/admin/subscriptions`
  (201; rejects non-HTTP(S) URLs and short secrets at validation),
  `GET /v1/admin/subscriptions` (list), `DELETE /v1/admin/subscriptions/{id}`
  (204; 404 unknown). Responses **never** include the signing secret.
- **`schemas/admin.py`** — `SubscriptionCreateRequest` (URL-scheme validator),
  `SubscriptionPublic`, `SubscriptionListResponse`.
- **Settings** (non-secret literals): `OUTBOX_DELIVERY_CRON_SECOND`,
  `OUTBOX_BATCH_LIMIT`, `OUTBOX_MAX_ATTEMPTS`, `OUTBOX_BACKOFF_BASE_SECONDS`,
  `OUTBOX_DELIVERY_TIMEOUT_SECONDS`; documented in `.example_env`.
- **`tests/test_outbox.py`**, **`tests/test_subscriptions.py`**, and an extended
  `tests/test_maintenance_worker.py` — transactional writes (incl. rollback drops
  the event), delivery happy-path with **signature verified by a fake subscriber**
  (`httpx.MockTransport`), retry/backoff, connection-error retry, max-attempts
  terminal, subscriber matching (filter / wildcard / inactive / multi-subscriber),
  due/limit claiming, and the admin 201/403/404/422 paths. **100% line + branch**.

### Changed

- Pins **`media-sdk-m8>=0.3.0`** for the new `OutboxEventPayload` webhook contract
  and adds **`httpx>=0.27.0`** (the delivery worker's outbound HTTP client).

### Notes

- The `m8_app` Alembic migration for the new `app_outbox_event` /
  `app_subscription` tables is **generated and applied automatically on
  `compose up`** (the hardened stack autoruns migrations); it is not hand-authored
  here.
- Delivery is at-least-once; subscribers dedupe on the event id and verify the
  `X-Signature` header.

## [0.0.6] — 2026-06-15 · Phase 15b · share links

Adds time-boxed, signed, shareable download links for a media object. The owner
mints and revokes them; anyone holding the token resolves it to a presigned
download — subject to the same antivirus scan-gating and after the visibility
rules from Phase 15a.

### Added

- **`db_models/share_tokens.py`** — `ShareToken` table (multi-tenant scope
  mirroring `StorageUsage`): `expires_at`, optional `max_uses` + `uses`
  counter, `revoked` flag, `created_at`. The `media_object_id` foreign key is
  **`ON DELETE CASCADE`** so Phase 14's hard-purge (a real `DELETE` on the
  parent object) drops dependent tokens instead of stranding them.
- **`schemas/shares.py`** — `ShareTokenCreate` (`expires_in?`, `max_uses?`),
  `ShareTokenPublic` (embeds the signed token), and `ShareTokenListResponse`.
  The caller chooses `expires_in`; the default and the upper bound are
  operator-configurable (`MEDIA_SHARE_DEFAULT_EXPIRES_SECONDS` /
  `MEDIA_SHARE_MAX_EXPIRES_SECONDS`), and a request above the cap is rejected
  (**422**).
- **`controllers/shares.py`** — `SharesController` with sync `@staticmethod`s:
  - `create` / `list_for_object` / `revoke` — owner-only (reuse `_load_object`
    ownership; superusers may also revoke).
  - `resolve` — verifies the HMAC signature, rejects expired / revoked /
    exhausted links (**403**), refuses objects that have not cleared antivirus
    scanning (**409**), records one use, and returns a short-lived presigned
    GET.
  - Tokens are HMAC-SHA256 authenticators over the row id, signed with a
    **dedicated, media-owned `MEDIA_SHARE_SIGNING_SECRET`** — kept independent of
    any auth-sdk token secret (e.g. `ACCESS_SECRET_KEY`) so the auth layer's key
    lifecycle/contract can never break share-link verification. Required setting
    (fails validation at startup if unset); rotation invalidates open links.
- **`app/routes/shares.py`** — `POST /v1/objects/{id}/shares` (201),
  `GET /v1/objects/{id}/shares`, `DELETE /v1/shares/{token_id}` (204), and the
  **public** `GET /v1/shares/{token}`; router registered in `app/main.py`.
- **`tests/test_shares.py`** — create / list / revoke, expiry, `max_uses`
  exhaustion, revoked + bad-signature rejection, scan-gating, and cascade-delete
  on hard-purge. 100% line + branch coverage.

### Notes

- New settings in `docker_compose/hardened_media_m8/media.env.example`: required
  `MEDIA_SHARE_SIGNING_SECRET` (literal `changethis`, complexity in the comment)
  plus the operator-tunable `MEDIA_SHARE_DEFAULT_EXPIRES_SECONDS` /
  `MEDIA_SHARE_MAX_EXPIRES_SECONDS`.
- The `m8_app` Alembic migration for the `share_token` table is **generated and
  applied automatically on `compose up`** (the hardened stack autoruns
  migrations); it is not hand-authored here.

## [0.0.5] — 2026-06-15 · Phase 14 · lifecycle, retention & orphan reconciliation

Introduces a **service-owned arq maintenance worker** — a second run-mode of the
*same* media-service image (no new build / Docker Hub publish), launched with a
command override. It owns the DB-coupled housekeeping jobs that must run on a
schedule with direct DB + storage access; `media-worker-m8` stays DB-free.

### Added

- **`controllers/maintenance.py`** — sync controller with three operations:
  - `hard_purge_expired` — the **true hard-delete** the API never performed
    (it only soft-deletes). Removes bytes (from the bucket *as stored*) **and**
    the row for DELETED objects past `MEDIA_RETENTION_PURGE_DAYS`, batch-bounded,
    with an execution-time invariant re-check guarding against a restore racing
    the delete. Does **not** re-debit quota (already debited at soft-delete).
  - `expire_stale_uploads` — thin pass-through to
    `AdminController.purge_stale_uploads` (single code path) for scheduled runs.
  - `reconcile_orphans` — both directions: DB-rows-without-bytes (report-only)
    and storage-keys-without-rows (opt-in repair). Excludes rows within a grace
    window and keys of in-flight (`INITIATED`) uploads.
- **`maintenance_worker.py`** — the only async surface: arq `WorkerSettings`
  reusing `core/arq.get_arq_redis_settings()`, with daily hard-purge / reconcile
  and hourly stale-expiry crons, each calling straight into the sync controller.
- **Admin routes** (superuser): `GET /v1/admin/maintenance/orphans` (report),
  `POST …/orphans/repair?confirm=true` (dry-run unless confirmed; only deletes
  storage-orphans), `POST …/maintenance/purge-expired` (operator parity). No new
  internal-HTTP / service-token surface — the worker runs in-process.
- **`schemas/maintenance.py`** — `HardPurgeResponse`, `OrphanRecord`,
  `OrphanReport`.
- **Settings** (not secrets — literal defaults): `MEDIA_RETENTION_PURGE_DAYS`,
  `MEDIA_PURGE_BATCH_LIMIT`, `MEDIA_RECONCILE_GRACE_MINUTES`,
  `MEDIA_RECONCILE_BATCH_LIMIT`, `MEDIA_PURGE_CRON_HOUR`, `MEDIA_STALE_CRON_MINUTE`.
- **Compose** — `media_service_worker` service: same image, command override to
  `arq media_service.maintenance_worker.WorkerSettings`, `deploy.replicas: 1`
  (single scheduler — prevents arq cron double-fire), hardened opts
  (`no-new-privileges`, `cap_drop: ALL`, `read_only`), runs **no** migrations.
- Pins **`media-sdk-m8>=0.2.0`** for the new `ObjectStorage.list_object_keys`
  primitive the orphan reconciler needs.

### Changed

- Bumped `arq>=0.28.0` (from `>=0.26.0`) — adds Python 3.14 support (the service
  Dockerfile base image) and pulls the cron-freeze (0.26.3) and task-retry
  race-condition (0.26.2) fixes, which directly harden the new
  `maintenance_worker` cron jobs and the producer pool; no API changes. Pinned
  `redis` to `>=5.3.1,<6.0.0`, making arq's hard `redis<6` constraint
  explicit/fail-closed.

### Notes

- **Audit** is structured-log-only this phase (no new model/migration); the
  immutable `audit_log` table is deferred to Phase 17.
- Destructive datetime comparisons run in SQL with an **aware** cutoff so they
  behave identically under SQLite (naive read-back) and Postgres (aware).

---

## [0.0.4] — 2026-06-14 · Phase 12 · worker backbone · antivirus · image variants · dynamic presets

Media-service becomes the **producer** for asynchronous background work handled
by `media-worker-m8`, consuming the shared `media-sdk-m8` for object storage and
the producer↔consumer job contracts.

### Added

- **SDK consumption** — `media-sdk-m8>=0.1.0` and `arq>=0.26.0` added to
  `requirements_base.txt`. `storage/client.py` is now a thin shim that builds an
  `ObjectStorageConfig` from `settings` and re-exports the SDK's `ObjectStorage`
  / `get_minio_client`; `app/deps.get_storage()` injects an SDK `ObjectStorage`
  from that config.
- **`core/arq.py`** — ARQ pool dependency (`get_arq_pool`, overridable in tests;
  real `create_pool` is live-only) plus `enqueue_scan` / `enqueue_variants`
  helpers built from `MEDIA_REDIS_*`.
- **Internal service auth** — `core/deps.require_service_token` compares
  `Authorization: Bearer <token>` to the new `MEDIA_INTERNAL_SERVICE_TOKEN`
  setting via `secrets.compare_digest` (**403** otherwise). New
  `app/routes/internal.py` router (all routes service-token guarded).
- **Antivirus flow** — `complete_upload` enqueues a `scan_object` job (object is
  `PENDING`); `POST /v1/internal/objects/{id}/scan-result` applies the verdict
  (CLEAN → `READY`/downloadable, else `QUARANTINED`; idempotent).
- **Download gating** — `…/download-url` now returns **409** until
  `scan_status == CLEAN`.
- **Image variants (producer)** — `core/media_types.py`
  (`is_processable_image` / `content_type_for_format`),
  `storage/keys.build_variant_key`, `db_models/variant_jobs.py` (`VariantJob`),
  `schemas/variants.py`, `controllers/variants.py`, and `app/routes/variants.py`:
  `POST /v1/objects/{id}/variants:generate` (202 + job, enqueues
  `generate_variants`), `GET …/variants`, `GET …/variants/jobs/{jid}`,
  `DELETE …/variants/{vid}`, plus internal register/job-status endpoints.
- **Dynamic presets** — `db_models/image_presets.py` (`ImagePreset`),
  `schemas/presets.py` (imgtools-free local mirror), `core/presets.py`
  (`BUILTIN_PRESETS` thumb/small/medium/large + `resolve_presets` that merges
  built-ins with user rows, user shadows same-named built-in, expands per format
  into `VariantSpec`s), `controllers/presets.py`, and `app/routes/presets.py`
  (`GET/POST /v1/presets`, `PATCH/DELETE /v1/presets/{id}`).
- **Tests** — `test_arq`, `test_internal_scan`, `test_scan_gating`,
  `test_media_types`, `test_variant_presets`, `test_variants_generate`,
  `test_variants_query`, `test_variants_internal`, `test_presets`, extended
  `test_storage_keys` / `test_core_deps`; `conftest` gains a `fake_arq_pool`
  override and a `service_client`. **100% line+branch coverage** maintained.

### Changed

- **`storage/client.py`** reduced to the SDK shim (the `ObjectStorage` wrapper
  now lives in and is tested by `media-sdk-m8`); `test_storage_client.py`
  rewritten to cover the shim only.
- **Config / env** — new `MEDIA_INTERNAL_SERVICE_TOKEN`, consumed by the
  `media_service` container via its `media.env` env_file (added to
  `media.env.example` as literal `changethis`, with entropy guidance in
  comments). It is a service-level secret, not a compose-interpolated one, so it
  is not added to the root `.env.example`.

> New tables `app_variant_job` and `app_image_preset` are created by an Alembic
> migration generated against the live database and applied automatically on
> compose up.

---

## [0.0.3] — 2026-06-13 · Phase 15a · access control — visibility & tenant enforcement

### Added

- **`require_visibility_access(obj, current_user)`** in
  `controllers/objects.py` — authorizes read/download by the object's
  `visibility`: owner and superusers always pass; `PUBLIC` is readable by any
  authenticated user; `TENANT` only by callers in the same (non-null) tenant;
  `PRIVATE`/`SENSITIVE` by nobody else (**403** otherwise). A null caller tenant
  never matches a `TENANT` object.
- **Tenant stamping at `initiate_upload`** — the upload session (and the object
  it promotes to) is now tagged with the caller's `tenant_id` taken from the
  authenticated principal's signed claim (never the request body). Quota checks
  are scoped to the same `(owner, tenant)`. This activates `TENANT` visibility
  and per-tenant quota end to end; untenanted callers yield `None` and behave
  exactly as before.
- **`tests/test_access_control.py`** — full visibility matrix over the helper,
  the `GET`/`download-url` routes, and list scoping (incl. tenant isolation);
  plus a tenant-stamping upload test.

### Changed

- **`GET /v1/objects/{id}` and `…/download-url`** now enforce `visibility`
  instead of bare ownership: previously every non-owner was refused, so `PUBLIC`
  and same-tenant `TENANT` objects were unreachable by users entitled to them.
- **`GET /v1/objects` (listing)** — a non-superuser now sees their own objects
  **plus** anything `PUBLIC` and same-tenant `TENANT` objects, mirroring
  `require_visibility_access` so the list never surfaces a row the caller could
  not also fetch by id. `PRIVATE`/`SENSITIVE` objects of other owners stay
  hidden. Owner-scoping and superuser scoping are unchanged.
- **Requirements** — `fastapi-m8` floor bumped `>=1.5.0` → `>=1.6.0`, which
  pins `auth-sdk-m8>=1.3.0` (the release that carries the `tenant_id` claim on
  `UserModel`). No new direct dependency.

> No schema change (`MediaObject.tenant_id` already existed). Tenant matching is
> now live wherever the auth layer issues a `tenant_id` claim; objects created
> by untenanted callers stay `tenant_id IS NULL` and `TENANT` behaves as
> owner/superuser-only for them.

---

## [0.0.3] — 2026-06-13 · Phase 13 · storage quotas & accounting

### Added

- **`db_models/storage_usage.py`** — `StorageUsage` table tracking
  `total_bytes` / `object_count` and optional `quota_bytes` / `quota_objects`
  overrides per `(owner_user_id, tenant_id)` scope, unique on the scope pair.
  (Alembic migration autogenerated at deploy.)
- **`core/quotas.py`** — single accounting helper used by every path that adds
  or removes stored bytes: `check_quota` (read-only enforcement),
  `record_object_added` / `record_object_removed` (uncommitted mutation in the
  caller's transaction), and `effective_quota_*` resolution (per-scope override
  else settings default). Removal totals clamp at zero so usage converges if
  accounting and storage ever drift.
- **Quota enforcement at `initiate_upload`** — refuses before issuing a
  presigned URL when the declared `expected_size_bytes` would exceed the byte
  quota (**413**) or the object-count quota (**409**).
- **Admin quota endpoints** — `GET`/`PUT /v1/admin/quotas/{owner_user_id}`
  (optional `?tenant_id=`) to read usage/effective quotas and set per-scope
  overrides; `GET /v1/admin/storage/stats` now also returns a per-owner `usage`
  list.
- **Metric** `media_uploads_quota_rejected_total{reason}` (`bytes` | `objects`).
- **Settings** `MEDIA_DEFAULT_QUOTA_BYTES` / `MEDIA_DEFAULT_QUOTA_OBJECTS`
  (both default unset = unlimited), documented in every `*.env.example`.

### Changed

- **`complete_upload`** credits, and **`delete_object`** debits, the owner's
  `StorageUsage` totals in the same transaction as the state change, so usage
  never diverges from the object set.

---

## [0.0.2] — 2026-06-12 · SD · auth event-stream consumer + platform alignment

> Tracks **`fastapi-m8 1.5.0`** / **`auth-sdk-m8 1.2.1`** / **`fa-auth-m8`** latest.

### Added

- **`core/events.py`** — wires fa-auth's private SSE bridge into the app lifespan
  via `build_event_stream_client` (`fastapi-m8 >= 1.5.0`). `handle_auth_event`
  dispatches on the signed `payload["event_type"]` (`session.revoked` →
  `evict_jti` / `evict_user`; `user.deleted` → `evict_user`); `handle_auth_gap`
  flushes the validation cache on an unresumable gap. Best-effort cache
  accelerator only — the JTI blacklist behind `INTROSPECTION_URL` stays
  authoritative and stream loss is non-fatal. The client starts only when
  `INTROSPECTION_URL` is configured. New tunables `EVENT_STREAM_CONNECT_TIMEOUT`
  / `EVENT_STREAM_READ_TIMEOUT` (inherited from `ConsumerServiceSettings`).

### Changed

- **`requirements_base.txt`** — `fastapi-m8` pin `>=1.4.0` → `>=1.5.0`, picking up
  the tiered response-header model (`auth-sdk-m8 1.2.1`). No service code change:
  `create_app` wires `add_security_headers_middleware` and the two new knobs
  (`HSTS_ENABLED`, `CONTENT_SECURITY_POLICY_ENABLED`) are inherited from
  `CommonSettings`.
- **Response security headers — tiered model.** HSTS and CSP, previously inferred
  from the production gate, are now **express opt-in** (both default off) and are
  **never emitted when `ENVIRONMENT=local`** even when enabled. Documented in
  README and every `.example_env` / `*.env.example`.
- **Env examples** (`media_service/.example_env`,
  `docker_compose/hardened_media_m8/{media,auth}.env.example`) — added the
  three-tier **Response security headers** block and the **Auth event stream**
  (SSE bridge) settings; corrected the stale "event bus not wired into any
  service yet" note now that the SSE consumer is live. `auth.env.example` gains
  the fa-auth publisher-side stream knobs (`EVENT_STREAM_ENABLED`,
  `EVENT_STREAM_BUFFER_SIZE`, `EVENT_STREAM_HEARTBEAT_SECONDS`,
  `EVENT_STREAM_MAX_QUEUE`).
- **Compose stack** (`docker_compose/hardened_media_m8`) — image bumps
  (PostgreSQL 18, Redis 8.8, Traefik v3.7.5, Prometheus 26.04); Traefik hardened
  to mirror `fa-auth-m8`: TLS 1.2 floor + strong ciphers, pinned `172.16.0.0/16`
  subnet with gateway-trust allowlists, dashboard on the loopback `traefik`
  entrypoint (`api.insecure=false`), encoded-character path hardening, and a
  longer DB `start_period` for slow first-boot init.

---

## [0.0.2] — 2026-06-12 · Phase 11 · upload validation & integrity hardening

### Added

- **`core/validation.py`** — pure content-validation helpers:
  - `sniff_mime(head)` — magic-byte MIME detection via `filetype`.
  - `mime_consistent(declared, sniffed)` — tolerant same-major check for
    `image/*`, `video/*`, `audio/*`; rejects cross-major spoofs.
  - `verify_sha256(data, expected)` — constant-time-comparable hex digest check.
  - `max_size_for_category(category)` — returns the category-specific limit from
    `MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY` or the global default.
- **`storage/client.py`** — two new `ObjectStorage` methods:
  - `get_object_head(*, bucket, object_key, length=512)` — reads a partial object
    for magic-byte sniffing.
  - `get_object(*, bucket, object_key)` — downloads an entire object for SHA-256
    verification.
- **`MediaObjectStatus.REJECTED`** — new enum member (no migration; `String(32)`
  column).
- **`media_uploads_rejected_total{reason}`** Prometheus counter in `metrics.py`;
  `inc_upload_rejected(reason)` helper.
- **`MEDIA_MAX_UPLOAD_SIZE_BYTES_PER_CATEGORY`** setting (`dict[str, int]`,
  default `{}`); parsed from a JSON environment variable.
- **Three validation checks in `complete_upload`** (after the `stat_object` call):
  1. Size enforcement — `stat.size > max_size_for_category(category)` → 422
     `size_exceeded`.
  2. Magic-byte MIME check — `sniff_mime(head)` inconsistent with declared type
     → 422 `mime_mismatch`; failure to read head silently skips the check.
  3. SHA-256 verification — when `req.sha256` is present, stream the full object
     and compare; mismatch → 422 `sha256_mismatch`; storage error → 422.
  On any rejection: upload session marked `ABORTED`, a `REJECTED` `MediaObject`
  is persisted for the audit trail, `media_uploads_rejected_total` is incremented.
- **`tests/test_upload_validation.py`** — 29 new tests covering all validation
  helpers and HTTP rejection/pass scenarios.

### Changed

- `requirements_base.txt` — added `filetype>=1.2.0`.
- `tests/test_metrics.py` — added noop + counter assertions for
  `inc_upload_rejected`; `test_setup_disabled` asserts `_uploads_rejected` is
  `None`.
- `tests/test_uploads.py` — `test_complete_upload_with_sha256` updated to provide
  a correct SHA-256 (now actually verified by the controller).
- `tests/test_storage_client.py` — added three tests for `get_object_head` and
  `get_object`.

---

## [0.0.1] — 2026-06-07 · Phase 10 · object listing, filtering & pagination

### Added

- **`GET /v1/objects`** — list endpoint with cursor pagination and filters
  (`controllers/objects.py`, `app/routes/objects.py`, `schemas/objects.py`).
  - Filters: `category`, `visibility`, `status`, `mime_prefix`,
    `created_from`/`created_to`, `q` (filename contains).
  - Sorting on `created_at` or `size_bytes`, `asc`/`desc`, with a stable
    secondary `id` tiebreak.
  - Opaque base64 keyset cursor (`next_cursor`); `limit` 1–100 (default 50);
    rate-limited at 120/min (`objects:list`).
  - Owner-scoped for regular users; superusers see all and may pass
    `owner_user_id` / `include_deleted`. Soft-deleted objects excluded by default.
- `ObjectListParams` / `ObjectListResponse` schemas.

### Changed

- `media_service/alembic/env.py` — `render_item` return type narrowed to
  `Literal[False]` so `mypy` reports zero issues.

### CI/CD

- **GitHub Actions added** (`.github/workflows/`), mirroring `fa-auth-m8` with
  SHA-pinned actions:
  - `CI.yaml` — `lint` (ruff format/check), `typecheck` (mypy), `security`
    (bandit + Trivy fs scan), and `test` (pytest matrix 3.11–3.14,
    `--cov-fail-under=100`, Codecov + Codacy upload).
  - `docker-publish.yaml` — multi-arch image build + Trivy image scan + push on
    release/dispatch.
- `mypy` added to `media_service/requirements_dev.txt` (required by the policy
  toolchain; the `typecheck` job depends on it).

### Tooling / quality config (mirrors `fa-auth-m8`)

- Added repo-root tool configs so `ruff`, `mypy`, `pytest`, and Codacy run with
  the same conventions as `fa-auth-m8`:
  - `ruff.toml` (line-length 88; excludes `docker_compose/` and
    `**/alembic/versions/`).
  - `mypy.ini` (`ignore_missing_imports`; excludes generated migrations).
  - `pytest.ini` — **`pythonpath = .`** (fixes `ModuleNotFoundError: media_service`
    in CI), `testpaths`, registered markers, and `addopts` (`--strict-markers`,
    `--ignore=tests/live`, `--cov=media_service`, `--cov-branch`).
  - `setup.cfg` (`pycodestyle max-line-length = 88`).
  - `.coveragerc` — `branch = True` + expanded `[report] exclude_lines`.
  - `.codacy.yml` — bandit/ruff/markdownlint engines; excludes `tests/**`,
    `docker_compose/**`, `**/alembic/versions/**`.
- **Branch coverage** now enforced; covered the slug-validator false branch in
  `db_models/categories.py`.
- **Codacy complexity fixes:** `list_objects` now takes `ObjectListParams` as a
  query-model dependency (15 → 3 params); `complete_upload` refactored under the
  50-line limit via shared `_load_owned_session` / `_ensure_completable` helpers
  (also reused by `abort_upload`).
- **Dockerfile:** pinned `pip==26.1.1` (matches `fa-auth-m8`).
- **`CLAUDE.md` untracked** (added to `.git/info/exclude`) — meta files are not
  version-controlled, consistent with `fa-auth-m8`.

Tested at 100% line+branch coverage (165 unit tests); ruff/mypy/bandit clean.

---

## [0.9.0] — 2026-06-07 · fastapi-m8 1.2.0 + docs/compose reconciliation

### Changed

- **`fastapi-m8` requirement bumped to `>=1.2.0`** (`media_service/requirements_base.txt`),
  which pulls in `auth-sdk-m8 1.0.0`. Under 1.0.0 the consumer is **secure-by-default**:
  - `TOKEN_STRICT_VALIDATION` defaults to `true` → `TOKEN_ISSUER` / `TOKEN_AUDIENCE`
    are required at boot (opt out with `TOKEN_STRICT_VALIDATION=false` for local dev).
  - `EVENT_SIGNING_ENABLED` defaults to `true` → a strong `EVENT_SIGNING_KEY` is
    required at boot or the process **fails closed**. Note: the auth-state event
    bus is not wired into any service yet, so this is a boot-time requirement
    only; the key is not used at runtime until the bus lands.
- **`README.md` rewritten** from the legacy `fa-media-m8` stub to a full overview
  aligned with the current code: API surface (uploads / objects / admin / category /
  dashboard), presigned upload flow, visibility→bucket mapping, auth modes, media
  Redis namespace, and custom metrics. Documents that `variants` are reserved stubs.
- **`media_service/.example_env` rebuilt** as a real media-consumer example
  (RS256/JWKS default, MinIO, `MEDIA_REDIS_*`, `EVENT_SIGNING_*`, boundary claims).
  It was previously a verbatim copy of the generic `fastapi_full` consumer example.

### Fixed

- **Metrics startup crash + missing `/metrics` endpoint** (`media_service/main.py`).
  `main.py` called `auth_sdk_m8.observability.metrics.setup()` itself, but `create_app`
  already calls it, so with `METRICS_ENABLED=true` the shared HTTP collectors registered
  twice and the app crashed with `Duplicated timeseries in CollectorRegistry:
  {'media_http_requests…'}`. The explicit call is removed (the media-specific counters are
  still registered), and — like the reference consumer — `main.py` now mounts the read-only
  `/metrics` endpoint, which previously was never registered despite the README/Prometheus
  expecting `/media/metrics`. Covered by two new tests (143 unit tests, 100% coverage).
- **Container boot crash** (`media_service/fastapi_pre_start.py`). The pre-start DB
  probe still imported `media_service.core.engine_sync` — a module that never existed
  after the fastapi-m8 1.1.0 migration moved the engine into `core/deps.py`. In Docker
  this raised `ModuleNotFoundError`, the container exited, and Traefik reported it
  could not find the media_service IP. The probe now uses the shared `DbEngine` from
  `core/deps.py` via its public `session()` API (the same engine the app uses).
- **Migration generation crash** (`media_service/alembic/env.py`). Autogenerated
  migrations referenced `media_service.core.db_models.UUIDString(...)` without
  importing the module, so `alembic upgrade` raised `NameError: name 'media_service'
  is not defined` and the container's DB init failed. Added a `render_item` hook
  (wired into both offline and online `context.configure`) that registers the import
  for any `media_service` custom column type, so generated scripts import it.
- **Test bootstrap for secure-by-default** (`tests/conftest.py`). Added the documented
  local opt-outs (`TOKEN_STRICT_VALIDATION=false`, `EVENT_SIGNING_ENABLED=false`) so the
  unit suite boots under auth-sdk-m8 1.0.0 without cross-service claim binding or a
  shared event-signing key. Suite remains 141 unit tests at 100% coverage.

### Removed

- **Legacy `slugify>=0.0.1`** dropped from `requirements_base.txt`. `python-slugify`
  (imported as `slugify`) is the sole slug dependency, matching the 0.5.0 migration.

### Compose (`docker_compose/hardened_media_m8`)

- **Added `media_redis_cache` service** (`redis:7.4-alpine`, `data_net` only) — the
  media-owned Redis the code (`core/media_redis.py`, `core/rate_limit.py`) targets via
  `MEDIA_REDIS_*`. Previously referenced by README/envs/code but never defined.
- **Added `minio-init` one-shot** — creates the five logical buckets and a scoped
  `media-rw` user/policy from the `media.env` credentials before `media_service` starts.
- **`media_service` now `depends_on` `minio`, `minio-init`, and `media_redis_cache`**;
  fixed the MinIO healthcheck to probe the in-container API port (`:9000`).
- **`auth.env.example` / `media.env.example` aligned** with the live `auth.env` /
  `media.env`: added the `EVENT_SIGNING` block and the `TOKEN_ISSUER`/`TOKEN_AUDIENCE`
  boundary-claim block.
- **README corrected**: directory name (`hardened_media_m8`), removed the inaccurate
  "media uses the auth Redis for revocation" claim (revocation is HTTP introspection),
  documented `minio-init` + `media_redis_cache`, and added `EVENT_SIGNING` setup notes.
- **Runtime secret env files untracked** — `.env`, `auth.env`, and `media.env` are now
  git-ignored (`*.env`) and removed from version control; only the `*.example` files
  remain tracked.
- **`init.sh --reset-db` fixed** (`docker_compose/shared/scripts/init-common.sh`).
  `rm -rf db_data/` failed with `Permission denied` because the directory is owned by the
  Postgres container's uid (0700); it now falls back to a throwaway root container to
  delete container-owned bind-mount data. The env bootstrap loop also matched only
  `.env`/`auth.env`/`api.env`, so `media.env` was never created — it now copies every
  `*.env.example` (dotglob-aware), so `media.env` is bootstrapped for this stack.

---

## [0.8.4] — 2026-06-05 · Shell script permissions + Traefik security hardening

### Fixed

- **All `.sh` scripts now stored as `100755` in git** (`media_service/scripts/`,
  `docker_compose/hardened_media_m8/init.sh`, `docker_compose/shared/`).
  Files were stored as `100644`; on hosts with `core.filemode=false` (WSL2, Windows,
  CI runners) the missing execute bit caused `Permission denied` from bind-mounted volumes.
  Fixed via `git update-index --chmod=+x` — independent of host `core.filemode`.

- **Traefik `auth-public-router` now excludes `/user/private/` and `/user/metrics`**
  (`docker_compose/hardened_media_m8/traefik/dynamic_conf.yml`).
  Both paths were reachable from the public internet. Traefik now returns 404 for
  these paths before requests reach the app.

- **Traefik `media-public-router` now excludes `/media/health/` and `/media/metrics`**
  (`docker_compose/hardened_media_m8/traefik/dynamic_conf.yml`).
  Both internal-only endpoints were fully routed to the public internet.
  A SECURITY CONTRACT comment block documents excluded paths with pointers to live tests.

- **`fastapi-m8` requirement bumped to `>=1.1.3`** (`media_service/requirements_base.txt`).

### Added

- **`tests/live/test_security_live.py`** — live security test suite for the
  `hardened_media_m8` compose stack. Verifies Traefik-level blocks for:
  - Auth `/user/private/` (private inter-service API)
  - Auth `/user/metrics` (Prometheus endpoint)
  - Media `/media/metrics` (Prometheus endpoint)
  - Media `/media/health/` (readiness probe)
  Failures print `[TRAEFIK MISCONFIGURATION]` with the exact fix required.

---

## [0.8.2] — 2026-06-03 · Phase 9: Custom Prometheus metrics + fastapi-m8 1.1.0 migration

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
