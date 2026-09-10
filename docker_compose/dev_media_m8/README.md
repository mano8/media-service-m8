# dev_media_m8

Local dev stack for `auth_user_service` + `media_service`.

Same hardened posture as `hardened_media_m8` (PostgreSQL 18, two Redis instances
(auth + media), SeaweedFS S3 storage, Traefik, Prometheus, Grafana, RS256/JWKS
auth, container hardening, network segmentation), with two developer
conveniences:

- **`media_service` and `media_service_worker` are built from local source**
  (`../../media_service`) instead of pulling the published image.
- **The storage backend's S3 gateway is published on loopback**
  (`127.0.0.1:9005`) so you can reach it from the host while iterating. Unlike
  the old MinIO block, there is no console port — SeaweedFS's admin/filer
  surfaces are loopback-bound *inside* the container and are never published.

`auth_user_service` and `media_worker` still use the published Docker Hub images.

## Architecture

```text
Browser / Frontend
       |
       v
  Traefik :9000
       | app_net
       +--> /user/*  -> auth_user_service :8000  (RS256 issuer)
       +--> /media/* -> media_service :8000      (RS256 consumer via JWKS)

  media_service
       +--> PostgreSQL on data_net
       +--> auth_user_service private API (HTTP introspection) for token revocation
       +--> Media Redis on data_net for media queues/rate limits/cache
       +--> Object storage (SeaweedFS S3) on data_net
```

`app_net` is external-facing for Traefik, app services, and observability.
`data_net` is internal and has no gateway; DB, Redis, and storage are not
exposed through that network (the storage backend additionally publishes one
loopback-only S3 gateway port for dev convenience).

> **Token revocation:** the media service does **not** connect to the auth
> Redis. In `stateful` mode it queries the auth service's private introspection
> endpoint (`INTROSPECTION_URL` → `/user/private/v1/jti-status`) over HTTP. The
> auth Redis (`redis_cache`) is used only by `auth_user_service`.

## Services

| Service | Image/build | Local access |
| --- | --- | --- |
| traefik | `traefik:v3.7.5` | `:8000`, `:4430`, `127.0.0.1:9000`, `127.0.0.1:8080` |
| auth_user_service | `tepochtli/fa-auth-m8:2.2.0` | `/user` via Traefik |
| media_service | local `../../media_service` build | `/media` via Traefik |
| media_service_worker | local `../../media_service` build (arq command override) | internal — no port; lifecycle/outbox crons |
| media_worker | `tepochtli/media-worker-m8:0.4.1` | internal — enqueue-driven (scan + variants) |
| clamav | `clamav/clamav:1.5-debian13-slim` | internal `scan_net` only |
| m8_db | `postgres:18.4-alpine` | internal data network |
| redis_cache | `redis:8.8.0-alpine` | auth Redis — internal data network |
| media_redis_cache | `redis:8.8.0-alpine` | media Redis — internal data network |
| storage | `chrislusf/seaweedfs:4.45` | `127.0.0.1:9005` S3 gateway — admin/filer surfaces loopback-bound inside the container |
| storage-config | `alpine:3.21.3` | one-shot: writes the backend's static identity table before it boots |
| storage-init | `amazon/aws-cli:2.36.40` | one-shot: creates the five buckets + pins per-bucket CORS |
| prometheus | `ubuntu/prometheus:3.11-26.04_stable` | `127.0.0.1:9090` |
| grafana | `grafana/grafana:13.1.0-25530058790` | `127.0.0.1:3000` |

A one-shot `cert-init` (`alpine:3.21.3`) generates local TLS certs before
Traefik starts.

## Setup

From `docker_compose/dev_media_m8`:

```sh
cp .env.example .env
cp auth.env.example auth.env
cp media.env.example media.env
```

Edit `.env` (infrastructure / bootstrap):

```ini
DB_USER=<postgres-superuser>
DB_PASSWORD=<postgres-superuser-password>
AUTH_DB_USER=<auth-db-user>
AUTH_DB_PASSWORD=<auth-db-password>
AUTH_DB_NAME=auth_db
MEDIA_DB_USER=<media-db-user>
MEDIA_DB_PASSWORD=<media-db-password>
MEDIA_DB_NAME=media_db
REDIS_PASSWORD=<auth-redis-password>
MEDIA_REDIS_PASSWORD=<media-redis-password>
S3_ROOT_USER=<storage-admin-access-key>
S3_ROOT_PASSWORD=<storage-admin-secret>
S3_CORS_ALLOW_ORIGIN=http://localhost:5173,http://localhost:9000
```

Edit `auth.env` so its generic runtime DB values match the `AUTH_DB_*` triplet in
`.env`, and set its `REDIS_PASSWORD` to match `.env`. `auth_user_service` is the
only service that connects to the auth Redis.

Edit `media.env` so it matches the `MEDIA_DB_*` triplet in `.env`:

```ini
DB_DATABASE=media_db
DB_USER=<same-as-MEDIA_DB_USER>
DB_PASSWORD=<same-as-MEDIA_DB_PASSWORD>
S3_ENDPOINT=storage:8333
S3_ACCESS_KEY=<media-rw-user>
S3_SECRET_KEY=<media-rw-password>
MEDIA_REDIS_HOST=media_redis_cache
MEDIA_REDIS_PASSWORD=<same-as-MEDIA_REDIS_PASSWORD-in-.env>
```

`MEDIA_REDIS_*` is the media-owned Redis for queues, rate limits, locks, and
cache keys under the `media:*` namespace. `media.env` has **no** `REDIS_*`
(auth Redis) settings — revocation goes through HTTP introspection.

The `storage-config` one-shot writes the backend's identity table from these
credentials **before** the backend boots — SeaweedFS has no bootstrap-time
user-creation API, so `S3_ACCESS_KEY` / `S3_SECRET_KEY` here become the scoped
`media-rw` identity, not the storage admin user (`S3_ROOT_USER` in `.env`).

### Secure-by-default settings (auth-sdk-m8 ≥ 1.0.0)

Both `auth.env` and `media.env` ship with two boot-required blocks. Leaving them
unset makes the service **fail closed** at startup:

- **`TOKEN_ISSUER` / `TOKEN_AUDIENCE`** — required because
  `TOKEN_STRICT_VALIDATION` defaults to `true`. Use identical issuer/audience
  values across the auth service and every consumer (opt out with
  `TOKEN_STRICT_VALIDATION=false` for local-only experiments).
- **`EVENT_SIGNING_KEY`** — required because `EVENT_SIGNING_ENABLED` defaults to
  `true`. Use the **same** key in `auth.env` and `media.env`. It signs and
  verifies the auth event-stream payloads delivered over fa-auth's private SSE
  bridge (`media_service` consumes them to evict its validation cache early); set
  `EVENT_SIGNING_ENABLED=false` in both files to disable signing entirely.

Initialize keys and local certificates:

```sh
bash init.sh
```

On Windows, run this from Git Bash.

Start the stack:

```sh
docker-compose up -d --build
```

If your Docker install supports Compose v2, `docker compose up -d --build` is
equivalent.

## Object storage

The storage backend's S3 gateway is exposed only on loopback for local
development; its admin/filer surfaces are loopback-bound *inside* the
container and are never published:

| Endpoint | URL |
| --- | --- |
| S3 gateway | `http://127.0.0.1:9005` |

The `storage-init` one-shot service creates these logical buckets:

```text
public-media
private-media
sensitive-media
temp-media
archive-media
```

CORS on each bucket is scoped to `S3_CORS_ALLOW_ORIGIN` (`.env`) — never a
wildcard. The scoped `media-rw` identity itself is declared up front by
`storage-config` (SeaweedFS reads its identities once at startup, so there is
no bootstrap-time user-creation call to make). `media_service` waits for
`storage-init` to complete before starting and uses `S3_ACCESS_KEY` /
`S3_SECRET_KEY`, not the storage admin credentials.

## URLs

| What | URL |
| --- | --- |
| Auth docs | `http://localhost:9000/user/docs` |
| Media docs | `http://localhost:9000/media/docs` |
| JWKS | `http://localhost:9000/user/.well-known/jwks.json` |
| Media metrics | `http://localhost:9000/media/metrics` |
| Traefik dashboard | `http://localhost:8080` |
| Prometheus | `http://localhost:9090` |
| Grafana | `http://localhost:3000` |
| Storage S3 gateway | `http://127.0.0.1:9005` |

## Observability

Prometheus scrapes:

| Job | Target | Path |
| --- | --- | --- |
| traefik | `traefik:8082` | built-in metrics |
| auth_user_service | `auth_user_service:8000` | `/user/metrics` |
| media_service | `media_service:8000` | `/media/metrics` |

Grafana uses the local Prometheus datasource. Default local credentials are
controlled by `grafana/config.monitoring`.

## Configuration Notes

- `.env` is infrastructure/bootstrap config. It provisions `AUTH_DB_*` and
  `MEDIA_DB_*` through `../shared/db_init/init-db.sh`, and supplies the Redis and
  storage admin credentials used by the `redis_cache`, `media_redis_cache`, and
  storage-bootstrap services. The `storage` service itself takes its identities
  from the static `-s3.config` file, which `storage-config` generates into
  `seaweedfs/config/s3.json` (gitignored — it carries both credentials
  verbatim).
- `auth.env` and `media.env` are runtime application configs consumed by
  `auth-sdk-m8`. They use generic `DB_DATABASE`, `DB_USER`, `DB_PASSWORD` — do
  **not** replace those with the `MEDIA_DB_*` / `AUTH_DB_*` names.
- Only `auth_user_service` connects to the auth Redis (`redis_cache`). The media
  service reaches the auth service over HTTP (`INTROSPECTION_URL`) for revocation.
- Use `MEDIA_REDIS_*` (→ `media_redis_cache`) for media-owned runtime state.
- **Per-service scoped Redis ACLs (plan 6.x.1).** Each Redis bootstraps a scoped
  ACL user instead of an open `~* +@all`: `redis_cache` creates `auth` (locked to
  the auth service's own key prefixes) and `media_redis_cache` creates `media`
  (locked to the `media:*` namespace + the `arq:*` queue keys). Both grant only
  the command categories the apps use and deny `@dangerous`/admin; the `default`
  user is stripped to connection-only so the healthcheck `PING` still works.
  `REDIS_USER=auth` / `MEDIA_REDIS_USER=media` wire the apps to those users.
- `.env`, `auth.env`, and `media.env` hold secrets and are git-ignored (`*.env`);
  only the `*.example` files are tracked.
- The media service base path is `/media`.
- This dev stack builds `media_service` from source; the published-image
  equivalent is `hardened_media_m8`.

## Common Commands

```sh
docker-compose config
docker-compose up -d --build
docker-compose ps
docker-compose logs -f media_service
docker-compose logs -f storage-init
docker-compose down
```

Resetting the DB is destructive:

```sh
bash init.sh --reset-db --yes
```

`--reset-db` removes `db_data/` even when PostgreSQL owns it as the container
uid — it falls back to a throwaway root container, so no manual `sudo rm` is
needed on WSL2/Linux bind mounts. On every run `init.sh` also enforces
`chmod 600` on each runtime `*.env` file and private key.

## Troubleshooting

**`changethis` rejection on startup**: replace placeholder values in `.env`,
`auth.env`, and `media.env`.

**Service exits at boot complaining about `EVENT_SIGNING_KEY` or
`TOKEN_ISSUER`/`TOKEN_AUDIENCE`**: these are required under auth-sdk-m8 ≥ 1.0.0.
Set them (identically across auth + media), or set `EVENT_SIGNING_ENABLED=false`
/ `TOKEN_STRICT_VALIDATION=false` for local-only runs.

**Media service cannot connect to object storage**: inside Docker, use
`S3_ENDPOINT=storage:8333`. The **browser**, however, uses
`S3_PUBLIC_ENDPOINT` (`http://127.0.0.1:9005`) to reach the storage backend
directly for presigned uploads/downloads; this is distinct from the internal
`storage:8333` endpoint. This separation enables browser-direct Option A
uploads (presigned POSTs and GETs), which requires the public endpoint for the
signatures to validate correctly.

**`storage-init` fails or buckets are missing**: check
`docker-compose logs storage-init`. It waits for `storage` to be healthy, then
creates buckets and pins CORS.

**`storage` never starts and `storage-config` exited non-zero**: the identity
table was refused. `docker-compose logs storage-config` names which credential
was empty, still `changethis`, contained a character it will not embed in JSON
verbatim, or (`S3_ROOT_USER == S3_ACCESS_KEY`) collided with the app identity.

**DB user authentication fails**: confirm `media.env` `DB_USER` / `DB_PASSWORD`
match `.env` `MEDIA_DB_USER` / `MEDIA_DB_PASSWORD`. If `db_data/` already exists,
DB init will not rerun unless you reset it.

**Prometheus media target is down**: check `media_service` logs and confirm
`/media/metrics` is enabled with `METRICS_ENABLED=true`.
