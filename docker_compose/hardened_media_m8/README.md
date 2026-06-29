# hardened_media_m8

Local hardened stack for `auth_user_service` + `media_service`.

Includes PostgreSQL 18, two Redis instances (auth + media), MinIO, Traefik,
Prometheus, Grafana, RS256/JWKS auth integration, hardened containers, and
network segmentation.

Use this example while developing the media microservice. Other compose examples
are intentionally not aligned until this one is working.

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
       +--> MinIO on data_net
```

`app_net` is external-facing for Traefik, app services, and observability.
`data_net` is internal and has no gateway; DB, Redis, and MinIO are not exposed
through that network.

> **Token revocation:** the media service does **not** connect to the auth
> Redis. In `stateful` mode it queries the auth service's private introspection
> endpoint (`INTROSPECTION_URL` → `/user/private/v1/jti-status`) over HTTP. The
> auth Redis (`redis_cache`) is used only by `auth_user_service`.

## Services

| Service | Image/build | Local access |
| --- | --- | --- |
| traefik | `traefik:v3.7.5` | `:8000`, `:4430`, `127.0.0.1:9000`, `127.0.0.1:8080` |
| auth_user_service | `tepochtli/fa-auth-m8:0.9.9` | `/user` via Traefik |
| media_service | `tepochtli/media-service-m8:0.0.9` | `/media` via Traefik |
| media_service_worker | `tepochtli/media-service-m8:0.0.9` (arq command override) | internal — no port; lifecycle/outbox crons |
| media_worker | `tepochtli/media-worker-m8:0.2.0` | internal — enqueue-driven (scan + variants) |
| clamav | `clamav/clamav:1.5-debian13-slim` | internal `scan_net` only |
| m8_db | `postgres:18.4-alpine` | internal data network |
| redis_cache | `redis:8.8.0-alpine` | auth Redis — internal data network |
| media_redis_cache | `redis:8.8.0-alpine` | media Redis — internal data network |
| minio | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z.hotfix.7aa24e772` | internal data network — **no host port** |
| minio-init | `quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z` | one-shot: buckets + `media-rw` policy |
| prometheus | `ubuntu/prometheus:3.11-26.04_stable` | `127.0.0.1:9090` |
| grafana | `grafana/grafana:13.1.0-25530058790` | `127.0.0.1:3000` |

A one-shot `cert-init` (`alpine:3.21.3`) generates local TLS certs before
Traefik starts.

## Setup

From `docker_compose/hardened_media_m8`:

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
MINIO_ROOT_USER=<minio-root-user>
MINIO_ROOT_PASSWORD=<minio-root-password>
```

Edit `auth.env` so its generic runtime DB values match the `AUTH_DB_*` triplet in
`.env`, and set its `REDIS_PASSWORD` to match `.env`. `auth_user_service` is the
only service that connects to the auth Redis.

Edit `media.env` so it matches the `MEDIA_DB_*` triplet in `.env`:

```ini
DB_DATABASE=media_db
DB_USER=<same-as-MEDIA_DB_USER>
DB_PASSWORD=<same-as-MEDIA_DB_PASSWORD>
MINIO_HOST=minio
MINIO_PORT=9000
MINIO_ACCESS_KEY=<media-rw-user>
MINIO_SECRET_KEY=<media-rw-password>
MEDIA_REDIS_HOST=media_redis_cache
MEDIA_REDIS_PASSWORD=<same-as-MEDIA_REDIS_PASSWORD-in-.env>
```

`MEDIA_REDIS_*` is the media-owned Redis for queues, rate limits, locks, and
cache keys under the `media:*` namespace. `media.env` has **no** `REDIS_*`
(auth Redis) settings — revocation goes through HTTP introspection.

The `minio-init` one-shot provisions a MinIO user from `media.env`'s
`MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`, so set those to the media-rw credentials
you want (not the MinIO root user).

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

## MinIO

In the hardened stack MinIO is **not** published to the host — it has no
`ports:` mapping and is reachable only by the application services on the
internal `data_net` (security item 0.2 removed the public host-port exposure).

However, the browser accesses MinIO **indirectly** via a dedicated Traefik router
for presigned uploads/downloads. The storage router is configured on the
**`websecure`** (TLS) entrypoint, published as `https://storage.localhost` (mapped
to Traefik host port `4430`; use your FQDN in staging/production). The route
explicitly excludes `/minio/*` paths to prevent access to the admin API or console
(`:9001`); only the S3 data path (`/{bucket}/{key}`) is exposed.

Configuration:
- `MINIO_PUBLIC_ENDPOINT=https://storage.localhost` in `media.env`
- `MINIO_API_CORS_ALLOW_ORIGIN: "https://localhost:4430"` in minio environment (edit for your FQDN)
- Traefik router uses `passHostHeader: true` — **required** for presigned GET signatures
  to validate correctly (SigV4 binds the Host header).

For debugging, reach the console/API via `docker compose exec` or by temporarily
adding a loopback `ports:` mapping; the dev stack (`dev_media_m8`) keeps the
loopback ports for convenience.

The `minio-init` one-shot service creates these logical buckets:

```text
public-media
private-media
sensitive-media
temp-media
archive-media
```

It also creates and attaches a scoped `media-rw` policy/user for the media
service credentials from `media.env`. `media_service` waits for `minio-init` to
complete before starting and uses `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`, not
the MinIO root credentials.

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
  MinIO root passwords used by the `redis_cache`, `media_redis_cache`, and
  `minio` services via Compose interpolation.
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
- Other compose examples are not updated by this hardened example.

## Common Commands

```sh
docker-compose config
docker-compose up -d --build
docker-compose ps
docker-compose logs -f media_service
docker-compose logs -f minio-init
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

**Media service cannot connect to MinIO**: inside Docker, use `MINIO_HOST=minio`
and `MINIO_PORT=9000`. The hardened stack does not publish MinIO to the host, so
debug from inside the network (`docker compose exec`) rather than via a host
port.

**`minio-init` fails or buckets are missing**: check `docker-compose logs minio-init`.
It waits for MinIO to be healthy, then creates buckets and the `media-rw` user.

**DB user authentication fails**: confirm `media.env` `DB_USER` / `DB_PASSWORD`
match `.env` `MEDIA_DB_USER` / `MEDIA_DB_PASSWORD`. If `db_data/` already exists,
DB init will not rerun unless you reset it.

**Prometheus media target is down**: check `media_service` logs and confirm
`/media/metrics` is enabled with `METRICS_ENABLED=true`.
