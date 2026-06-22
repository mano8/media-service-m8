# Docker Compose Examples

Ready-to-run stacks for the `media-service-m8` microservice. Every stack runs
the same application services — `auth_user_service` (the `fa-auth-m8` issuer),
`media_service` + its maintenance worker, the DB-free `media_worker`, and
ClamAV — behind Traefik, with PostgreSQL, two Redis instances (auth + media),
and MinIO.

---

## Summary

- [Which stack should I use?](#which-stack-should-i-use)
- [Common architecture](#common-architecture)
- [Token modes](#token-modes)
- [Quick start](#quick-start)
- [Environment file system](#environment-file-system)
- [Database isolation](#database-isolation)
- [Shared migrations](#shared-migrations)
- [Ports](#ports-same-for-all-stacks)
- [Live testing](#live-testing)

---

## Which stack should I use?

| Stack | media_service / worker | fa-auth + media_worker | MinIO host ports | Best for |
| --- | --- | --- | --- | --- |
| [dev_media_m8](dev_media_m8/) | built from `../../media_service` | published images | `127.0.0.1:9005/9006` | Iterating on media-service against published peers |
| [hardened_media_m8](hardened_media_m8/) | `tepochtli/media-service-m8:0.0.9` | published images | none (internal only) | Reference deployment / production-shaped posture |
| [worspace_dev_media_m8](worspace_dev_media_m8/) | built from `../../` | built from sibling repos | `127.0.0.1:9005/9006` | Cross-repo workspace dev (local-only, not in CI) |

**Decision guide:**

- **Develop media-service against stable peers** → [dev_media_m8](dev_media_m8/)
- **Run the reference, fully-pinned, published-image stack** → [hardened_media_m8](hardened_media_m8/)
- **Hack on `fa-auth-m8` / `media-worker-m8` / `media-service-m8` together** →
  [worspace_dev_media_m8](worspace_dev_media_m8/) (requires the sibling repos
  checked out alongside this one)

All three default to RS256/JWKS auth and `TOKEN_MODE=stateful` (HTTP
introspection). `dev_media_m8` and `hardened_media_m8` are the pinned stacks
asserted by the compose-policy tests; `worspace_dev_media_m8` is local-only
(runtime data + `*.env` git-ignored) and intentionally excluded from CI.

---

## Common architecture

All stacks share the same service layout:

```text
Browser / Frontend
       │
       ▼
  Traefik :9000
       │ app_net
       ├──> /user/*  ─> auth_user_service :8000  (RS256 issuer)
       └──> /media/* ─> media_service :8000      (RS256 consumer via JWKS)

  media_service ─┬─> PostgreSQL (data_net)
                 ├─> media Redis (data_net) — queues / rate limits / cache
                 ├─> MinIO (data_net)
                 └─> auth_user_service private API (HTTP introspection)

  media_worker ──> ClamAV (scan_net) + media_service internal API (callbacks)
```

Traefik is the single host entry point. Application services sit on `app_net`;
PostgreSQL, Redis, and MinIO live on the internal `data_net`; the worker↔ClamAV
traffic is isolated on `scan_net`.

---

## Token modes

Set `TOKEN_MODE` in `auth.env` (and match it in `media.env`) to control how
access tokens are validated:

| Mode | How it works | Redis for JWT | Use case |
| --- | --- | --- | --- |
| `stateless` | Verify JWT signature only — no server-side state | No | Maximum scalability, no revocation |
| `hybrid` | JWT access token + Redis-stored refresh allowlist | Refresh only | Scalable access + revocable refresh |
| `stateful` | Every request checks revocation via HTTP introspection | Yes (auth) | Instant logout guarantee (stack default) |

> The media service never connects to the auth Redis — in `stateful` mode it
> reaches `auth_user_service`'s private introspection endpoint over HTTP.

---

## Quick start

Every stack follows the same steps (run from the stack directory):

```sh
# 1. Copy env files and fill in all secrets (replace every 'changethis')
cp .env.example .env
cp auth.env.example auth.env
cp media.env.example media.env

# 2. Generate keys (RS256) and local TLS certificates
bash init.sh

# 3. (Optional) Reset the database volume if it already exists
# bash init.sh --reset-db --yes

# 4. Bring up the stack — migrations run automatically on startup
docker compose up -d --build
```

> **Windows:** `init.sh` requires bash — use **Git Bash** or **WSL**.

Generate secret values with:

```sh
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

---

## Environment file system

Each stack uses a stack-root `.env` plus per-service runtime env files. Copy the
`.example` files and fill in your values:

```text
.env            ← infrastructure/bootstrap: DB provisioning + Redis/MinIO root passwords
auth.env        ← auth_user_service: algorithm, token mode, secrets, DB/Redis config
media.env       ← media_service + workers: DB, MinIO, media Redis, share/internal secrets
worker.env      ← media_worker: queue + internal-callback token
test.env        ← security-tests-m8 live suite (see Live testing)
```

Only the `*.example` files are tracked; the real `*.env` files hold secrets and
are git-ignored. Secrets use the literal placeholder `changethis`, which the
services reject at boot (fail-closed).

---

## Database isolation

`../shared/db_init/init-db.sh` runs inside the DB container on first volume
creation and provisions databases automatically from the `AUTH_DB_*` and
`MEDIA_DB_*` triplets in `.env` (per-service isolation: separate database +
credentials for auth and media). Database provisioning runs **once** on first
volume creation; if DB config changes after the volume exists, reset with
`bash init.sh --reset-db`.

---

## Shared migrations

Alembic migrations for both schemas are applied automatically every time the
containers start (the hardened stack applies them on startup; see each stack's
README). Migration version files are never hand-authored — they are generated
via Alembic autogenerate against a real database.

---

## Ports (same for all stacks)

| Port | Bound to | What |
| --- | --- | --- |
| `8000` | `0.0.0.0` | Traefik HTTP — public |
| `4430` | `0.0.0.0` | Traefik HTTPS — public |
| `9000` | `127.0.0.1` | API services entry (Traefik) |
| `8080` | `127.0.0.1` | Traefik dashboard |
| `5432` | `127.0.0.1` | PostgreSQL |
| `9005` / `9006` | `127.0.0.1` | MinIO API / console (`dev_media_m8`, `worspace_dev_media_m8` only) |
| `9090` | `127.0.0.1` | Prometheus |
| `3000` | `127.0.0.1` | Grafana |

Port `9000` is the one you'll use most in development — all API requests go
through it.

---

## Live testing

Every stack ships a `test.env.example` wired for [`security-tests-m8`](https://github.com/mano8/security-tests-m8) — a reusable live security suite that attacks the *running* stack (auth bypass, token forgery, `alg=none`, JWKS/algorithm confusion, privilege escalation, OWASP API Top 10). These flaws only surface end-to-end against a fully wired deployment — here, the `fa-auth-m8` issuer plus the `media-service-m8` consumer behind Traefik — not in unit tests. Run it after `docker compose up` and after any auth/token/network/image change.

**Recommended — CLI mode** (excludes destructive tests by default):

```sh
pip install --upgrade security-tests-m8

cd <stack>/                  # e.g. hardened_media_m8
cp test.env.example test.env
# Edit test.env: point LIVE_TEST_ADMIN_EMAIL / LIVE_TEST_ADMIN_PASSWORD at a
# DEDICATED test-only superuser — it must already exist, and must NOT be the
# bootstrap FIRST_SUPERUSER (preflight refuses that). Fill or remove the opt-in
# secret lines; never leave 'changethis' in test.env.

security-tests-m8 preflight --deployment-root .
security-tests-m8 run --env-file test.env
# Full mutation-heavy run: add --include-destructive
```

The suite auto-detects the stack's algorithm and token mode and skips checks that don't apply, so the same workflow covers every stack here. **Clean up afterward:** the suite does not delete the dedicated test superuser (or the `redteam_*` users it creates) — remove or disable them after a run on any shared or long-lived stack.

**Advanced — pytest mode.** For local marker selection, custom tests, or suite extension, use [`shared_live_tests/`](shared_live_tests/), which also documents the full rationale: why a dedicated superuser, when to run, and cleanup.

For a manual smoke test, check the health endpoint after `docker compose up`:

```sh
curl http://localhost:9000/media/health/
# Expected: {"status":"ok",...}
```

Then open `http://localhost:9000/media/docs` in a browser (requires
`SET_DOCS=true` in `media.env`).
