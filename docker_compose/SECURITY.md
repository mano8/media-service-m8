# Security — media-service-m8 Docker Compose Examples

Operational security reference for the stacks in `docker_compose/`.

For the underlying SDK security model (cryptographic primitives, config-health guards, app-layer
access guards, per-consumer credential verification) see the
[auth-sdk-m8 SECURITY.md](https://github.com/mano8/auth-sdk-m8/blob/main/SECURITY.md).
For the consumer-framework transport guidance see the
[fastapi-m8 SECURITY.md](https://github.com/mano8/fastapi-m8/blob/main/SECURITY.md).

---

## Trust model

This compose tree runs two application services behind a shared Traefik entry point:

- **`auth_user_service`** (`fa-auth-m8`) — the authentication authority: token issuance, session
  state, JWKS publication, and the private API (`/user/private/*`).
- **`media_service`** (`media-service-m8`) — the media authority: object storage, signed URLs,
  and image processing. Validates access tokens locally (JWKS) and queries `auth_user_service`'s
  private introspection endpoint for revocation.
- **`media_worker`** (`media-worker-m8`) — async worker: processes scan and variant jobs from the
  media Redis queue and reports results to `media_service` via its internal HTTP API.

### Inter-service paths

| Caller | Callee | Transport | Auth |
| --- | --- | --- | --- |
| `media_service` | `auth_user_service /user/private/v1/jti-status` | HTTP (Docker `app_net`) | `X-Internal-Client` + `X-Internal-Token` (per-consumer credential, item 9.1) |
| `media_service` | `auth_user_service /user/private/v1/events/stream` | HTTP SSE (Docker `app_net`) | same per-consumer credential |
| `media_worker` | `media_service` internal API | HTTP (Docker `data_net`) | `Bearer MEDIA_INTERNAL_SERVICE_TOKEN` |

All three paths are **internal Docker-network-only** and are never exposed on the public Traefik
entrypoint. The app-layer credential check is the **primary** access control on each path;
network topology is defense-in-depth.

### Network segmentation

```text
Internet
    │
    ▼ :8000 (HTTP) / :4430 (HTTPS)
 Traefik  ──── websecure entryPoint
    │
    ├── /user/*   →  auth_user_service :8000  (app_net)
    └── /media/*  →  media_service :8000      (app_net)

 Traefik  ──── api entryPoint :9000 (loopback-bound — internal/metrics only)
    └── /user/private/*    (X-Internal-Token)
        /user/metrics      (scrape credential)
        /media/private/*   (X-Internal-Token)
        /media/metrics     (scrape credential)

 data_net (internal, no gateway):
    auth_user_service ←→ m8_db, redis_cache
    media_service     ←→ m8_db, media_redis_cache, minio
    media_worker      ←→ media_redis_cache, minio, media_service (internal callback)

 scan_net (internal, no gateway):
    media_worker ←→ clamav (TCP :3310 only)

 clamav_egress:
    clamav only — freshclam DB updates; no other container attached
```

`media_worker` and `clamav` are **off `app_net`** entirely: the worker runs no HTTP server,
publishes no host port, and its only outbound HTTP path is the `media_service` internal callback
over `data_net`. Traefik never sees worker or clamav traffic.

---

## Service identity and mTLS (multi-host deployments)

The app-layer credential check (`X-Internal-Client` + `X-Internal-Token`, or `Bearer`
service-token) is always the **primary** control on every inter-service path. For the transport
beneath it, follow the canonical
[**"Service identity and mTLS"**](https://github.com/mano8/auth-sdk-m8/blob/main/SECURITY.md#service-identity-and-mtls-multi-host-deployments)
section of `auth-sdk-m8`'s `SECURITY.md` — it carries the Traefik internal-entrypoint
client-cert reference config, the CA/cert generation steps, and the service-mesh alternative.

### Single trusted Docker host

When all services run on one machine, the Docker bridge network (`app_net`, `data_net`) is the
isolation boundary — equivalent to a single-host kernel namespace. Internal `http://` between
`media_service` and `auth_user_service`'s private entrypoint is acceptable in this topology.
The `ALLOW_INTERNAL_HTTP` guard warns in production but does not block; `local` is unrestricted.

### Multi-host / untrusted network

When `auth_user_service` and `media_service` run on different machines (or across a network that
is not a single trusted Docker bridge), the container network no longer provides kernel-level
isolation. In this case:

- Add **mTLS** on the path from `media_service` to `auth_user_service`'s private entrypoint
  (`/user/private/*`): `media_service` presents a client certificate that Traefik verifies with
  `RequireAndVerifyClientCert` on the internal entrypoint. Mount the client cert/key and the CA
  into the `media_service` container per the auth-sdk-m8 reference config.
- Protect the `media_worker` → `media_service` internal callback the same way if those two
  services are split across hosts.
- The same pattern applies to any consumer that calls `auth_user_service`'s event stream
  (`/user/private/v1/events/stream`).

### Defense in depth, not a replacement

Keep the app-layer credential check enabled **alongside** mTLS:

- If cert rotation lapses, the token check still gates access.
- If a token leaks over an unencrypted hop, mTLS encrypts the channel.

Internal HTTPS is **not** a blanket mandate — mTLS is the *multi-host* transport control. On a
single trusted Docker host the network boundary is sufficient.

---

## Reporting a vulnerability

Report security vulnerabilities privately through GitHub's **Security** tab on this repository —
**"Report a vulnerability"** — which opens a private security advisory visible only to the
maintainers. Do not open a public GitHub issue for vulnerabilities. Expected response within 48 h.
