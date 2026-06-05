# hardened_m8

Local hardened stack for `auth_user_service` + `media_service`.

Includes PostgreSQL 16, Redis, MinIO, Traefik, Prometheus, Grafana, RS256/JWKS auth integration, hardened containers, and network segmentation.

Use this example while developing the media microservice. Other compose examples are intentionally not aligned until this one is working.

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
       +--> Auth Redis on data_net for token revocation checks
       +--> Media Redis on data_net for media queues/rate limits/cache
       +--> MinIO on data_net
```

`app_net` is external-facing for Traefik, app services, and observability. `data_net` is internal and has no gateway; DB, Redis, and MinIO are not exposed through that network.

## Services

| Service | Image/build | Local access |
| --- | --- | --- |
| traefik | `traefik:v3.3` | `:8000`, `:4430`, `127.0.0.1:9000`, `127.0.0.1:8080` |
| auth_user_service | `tepochtli/fa-auth-m8:latest` | `/user` via Traefik |
| media_service | local `../../media_service` build | `/media` via Traefik |
| m8_db | `postgres:16-alpine` | internal data network |
| redis_cache | `redis:7.4-alpine` | internal data network |
| media_redis_cache | `redis:7.4-alpine` | internal data network |
| minio | `quay.io/minio/minio` | `127.0.0.1:9005` API, `127.0.0.1:9006` console |
| prometheus | `ubuntu/prometheus:3.11-24.04_stable` | `127.0.0.1:9090` |
| grafana | `grafana/grafana:13.1.0` | `127.0.0.1:3000` |

## Setup

From `docker_compose/hardened_m8`:

```sh
cp .env.example .env
cp auth.env.example auth.env
cp media.env.example media.env
```

Edit `.env`:

```ini
DB_PASSWORD=<postgres-root-password>
AUTH_DB_USER=<auth-db-user>
AUTH_DB_PASSWORD=<auth-db-password>
AUTH_DB_NAME=auth_db
MEDIA_DB_USER=<media-db-user>
MEDIA_DB_PASSWORD=<media-db-password>
MEDIA_DB_NAME=media_db
REDIS_PASSWORD=<redis-password>
MEDIA_REDIS_PASSWORD=<media-redis-password>
MINIO_ROOT_USER=<minio-root-user>
MINIO_ROOT_PASSWORD=<minio-root-password>
```

Edit `auth.env` so its generic runtime DB values match the `AUTH_DB_*` triplet in `.env`.

Edit `media.env` so its generic runtime DB values match the `MEDIA_DB_*` triplet in `.env`:

```ini
DB_DATABASE=media_db
DB_USER=<same-as-MEDIA_DB_USER>
DB_PASSWORD=<same-as-MEDIA_DB_PASSWORD>
MINIO_HOST=minio
MINIO_PORT=9000
MINIO_ACCESS_KEY=<media-rw-user>
MINIO_SECRET_KEY=<media-rw-password>
REDIS_HOST=redis_cache
REDIS_PASSWORD=<same-as-REDIS_PASSWORD-in-.env>
MEDIA_REDIS_HOST=media_redis_cache
MEDIA_REDIS_PASSWORD=<same-as-MEDIA_REDIS_PASSWORD-in-.env>
```

`REDIS_*` remains the auth Redis connection used by `auth-sdk-m8` for stateful access-token revocation checks. `MEDIA_REDIS_*` is reserved for media-owned queues, rate limits, locks, and cache keys under the `media:*` namespace.

Initialize keys and local certificates:

```sh
bash init.sh
```

On Windows, run this from Git Bash.

Start the stack:

```sh
docker-compose up -d --build
```

If your Docker install supports Compose v2, `docker compose up -d --build` is equivalent.

## MinIO

MinIO remains exposed only on loopback for local development:

| Endpoint | URL |
| --- | --- |
| API | `http://127.0.0.1:9005` |
| Console | `http://127.0.0.1:9006` |

The `minio-init` one-shot service creates these logical buckets:

```text
public-media
private-media
sensitive-media
temp-media
archive-media
```

It also creates/attaches a limited `media-rw` policy for the media service credentials from `media.env`. The media service should use `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`, not MinIO root credentials.

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
| MinIO console | `http://127.0.0.1:9006` |

## Observability

Prometheus scrapes:

| Job | Target | Path |
| --- | --- | --- |
| traefik | `traefik:8082` | built-in metrics |
| auth_user_service | `auth_user_service:8000` | `/user/metrics` |
| media_service | `media_service:8000` | `/media/metrics` |

Grafana uses the local Prometheus datasource. Default local credentials are controlled by `grafana/config.monitoring`.

## Configuration Notes

- `.env` is infrastructure/bootstrap config. It provisions `AUTH_DB_*` and `MEDIA_DB_*` through `../shared/db_init/init-db.sh`.
- `auth.env` and `media.env` are runtime application configs consumed by `auth-sdk-m8`.
- Runtime service env files intentionally use generic `DB_DATABASE`, `DB_USER`, and `DB_PASSWORD`.
- Do not replace the SDK-compatible runtime DB variables with `MEDIA_DB_*` inside `media.env`.
- Do not point `media.env` `REDIS_*` at `media_redis_cache` while `TOKEN_MODE=stateful`; that would disconnect media from auth token revocation state.
- Use `MEDIA_REDIS_*` for media-owned runtime state.
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

## Troubleshooting

**`changethis` rejection on startup**: replace placeholder values in `.env`, `auth.env`, and `media.env`.

**Media service cannot connect to MinIO**: inside Docker, use `MINIO_HOST=minio` and `MINIO_PORT=9000`. Host ports `9005` and `9006` are only for local browser/tool access.

**DB user authentication fails**: confirm `media.env` `DB_USER` / `DB_PASSWORD` match `.env` `MEDIA_DB_USER` / `MEDIA_DB_PASSWORD`. If `db_data/` already exists, DB init will not rerun unless you reset it.

**Prometheus media target is down**: check `media_service` logs and confirm `/media/metrics` is enabled with `METRICS_ENABLED=true`.
