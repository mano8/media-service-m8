# Shared live security test (media-service hardened reference)

This example runs the full `security-tests-m8` live security suite against the `media-service-m8` hardened Docker Compose stack.

> **Reusable, not hardened-only.** This is the *shared* live test: the same
> folder runs against **any compose stack that uses `fa-auth-m8` as the issuer
> and `fastapi-m8`-based consumers** (here, `media-service-m8`). Only the
> configuration in `.env` changes — see
> [Adapting To Another Stack](#adapting-to-another-stack). `hardened_media_m8`
> is the reference target documented below.

- Tested compose stack on GitHub: [`mano8/media-service-m8/docker_compose/hardened_media_m8`](https://github.com/mano8/media-service-m8/tree/main/docker_compose/hardened_media_m8)
- Local compose stack path: `/workspace/media-service-m8/docker_compose/hardened_media_m8`
- Local live-test example folder: `/workspace/media-service-m8/docker_compose/shared_live_tests`
- Canonical package example: [`mano8/security-tests-m8/examples/hardened_m8_full_security`](https://github.com/mano8/security-tests-m8/tree/main/examples/hardened_m8_full_security)

It is built for the default hardened stack routes:

- auth service: `http://localhost:9000/user`
- downstream media service: `http://localhost:9000/media` (media-service `API_PREFIX=/media`)
- public HTTPS entrypoint: `https://localhost:4430`
- stack root and JWT keys: `/workspace/media-service-m8/docker_compose/hardened_media_m8`

The live tests require a dedicated test-only superuser. Do not use `FIRST_SUPERUSER` / `FIRST_SUPERUSER_PASSWORD` from `auth.env`; the package preflight refuses that by default.

CLI mode is recommended for normal users and excludes destructive tests by default. This local pytest example is for custom tests, extra marker selection, and local suite extension. The unknown-route information-disclosure test now lives in the package full suite and no longer needs to be copied into this folder.

## What It Runs

The example includes:

- universal auth security suites
- stateful/stateless/hybrid contract suites
- RS256/JWKS/cross-service JWT suites
- HS256 rejection and weak-key suites
- protected-endpoint checks for the media-service read endpoints, configured via
  `LIVE_TEST_PROTECTED_ENDPOINTS` (all paths are relative to `/media`):
  - `/category/`
  - `/dashboard/users/activity/`
  - `/dashboard/users/activity/current/`
  - `/v1/objects`
  - `/v1/presets`
  - `/v1/admin/storage/stats`
  - `/v1/admin/uploads/stale`
  - `/v1/admin/maintenance/orphans`
  - `/v1/admin/subscriptions`

Each configured endpoint is checked for: no token → `401`/`403`, an invalid
bearer token → `401`/`403`, and the dedicated admin token → `200`. Only
authenticated `GET` endpoints with no required path parameters are listed;
resource-scoped routes (for example `/v1/objects/{object_id}` or
`/v1/admin/quotas/{owner_user_id}`) and mutating routes (`uploads`, `variants`,
`shares`) are not covered by the generic suite. Add them in your own subclass if
you need them.

The hardened stack is RS256 and stateful, so pytest automatically skips suites that do not apply to that detected stack.

## Files

```text
examples/docker_compose/shared_live_tests/
├── env.example
├── pytest.ini
├── README.md
└── tests/live/
    ├── conftest.py
    └── test_full_security.py
```

## Start The Hardened Stack

From the hardened stack directory:

```bash
cd /workspace/media-service-m8/docker_compose/hardened_media_m8
cp .env.example .env
cp auth.env.example auth.env
cp api.env.example api.env
bash init.sh
docker compose up -d
```

Before running the live tests, create a dedicated superuser for the test suite. Put that account in the live-test env file you use for the run:

```ini
LIVE_TEST_ADMIN_EMAIL=tester@example.com
LIVE_TEST_ADMIN_PASSWORD=change-this-test-password
```

The account must already exist in the auth stack and must have superuser permissions.

## Run With The Recommended CLI Mode

Install `security-tests-m8` in editable mode:

```bash
cd /workspace/security-tests-m8
pip install -e .
```

From the hardened stack directory, keep stack configuration in `.env`, `auth.env`, `api.env`, `media.env`, and `grafana/.env`, then create a dedicated `test.env` for the live-test runner values:

```bash
cd /workspace/media-service-m8/docker_compose/hardened_media_m8
cp test.env.example test.env
# Edit test.env with the dedicated test account and, if used, real opt-in secrets.
security-tests-m8 preflight --deployment-root .
security-tests-m8 run --env-file test.env
# Optional full mutation-heavy run:
security-tests-m8 run --env-file test.env --include-destructive
```

Deployment preflight scans non-example `*.env` files under the deployment root, including `test.env` if you keep it there. Do not leave `changethis` or other placeholder values in `test.env`; either replace the opt-in secret values with the real values from `auth.env` / `api.env`, or omit those variables to skip their opt-in checks.

## Run This Advanced Pytest Example

Use this folder when you want local pytest customization, marker selection, or extra local tests layered on top of the reusable package suite.

Copy the example env file, edit the dedicated test credentials, then run pytest from this directory. `tests/live/conftest.py` calls `configure_from_env()`, so the package loads `.env` from the current directory automatically:

```bash
cd /workspace/media-service-m8/docker_compose/shared_live_tests
cp env.example .env
pytest
```

Useful marker selections:

```bash
pytest -m live
pytest -m "live and not destructive"
pytest -m live_asymmetric
pytest -m live_stateful
```

## Configuration Values

The example defaults are defined in `tests/live/conftest.py` and can be overridden with environment variables.

| Variable | Example value |
| --- | --- |
| `LIVE_TEST_AUTH_BASE` | `http://localhost:9000/user` |
| `LIVE_TEST_SVC_BASE` | `http://localhost:9000/media` |
| `LIVE_TEST_SVC_BASES` | `{"media":"http://localhost:9000/media"}` |
| `LIVE_TEST_DEFAULT_SVC` | `media` |
| `LIVE_TEST_ADMIN_EMAIL` | `tester@example.com` |
| `LIVE_TEST_ADMIN_PASSWORD` | `change-this-test-password` |
| `LIVE_TEST_PUBLIC_BASE` | `https://localhost:4430` |
| `LIVE_TEST_PUBLIC_TLS_VERIFY` | `false` |
| `LIVE_TEST_PRIVATE_API_SECRET` | real `PRIVATE_API_SECRET`, or unset |
| `LIVE_TEST_REFRESH_SECRET_KEY` | real `REFRESH_SECRET_KEY`, or unset |
| `LIVE_TEST_FAIL_FAST_PREFLIGHT` | `true` |
| `LIVE_TEST_FORBID_BOOTSTRAP_SUPERUSER` | `true` |
| `LIVE_TEST_PROTECTED_ENDPOINTS` | `{"media":["/category/","/dashboard/users/activity/","/dashboard/users/activity/current/","/v1/objects","/v1/presets","/v1/admin/storage/stats","/v1/admin/uploads/stale","/v1/admin/maintenance/orphans","/v1/admin/subscriptions"]}` |
| `LIVE_TEST_REPO_ROOT` | `/workspace/media-service-m8/docker_compose/hardened_media_m8` |
| `LIVE_TEST_DEPLOYMENT_ROOT` | `/workspace/media-service-m8/docker_compose/hardened_media_m8` |

`LIVE_TEST_REPO_ROOT` lets asymmetric-key tests inspect the hardened stack's generated `keys/private.pem` and `keys/public.pem` files.
`LIVE_TEST_PRIVATE_API_SECRET` and `LIVE_TEST_REFRESH_SECRET_KEY` are opt-in secret-exposure checks. If they are unset, those specific tests skip.

## Adapting To Another Stack

Nothing in this folder is specific to `hardened_media_m8` beyond the values in
`.env`. To target a different `fa-auth-m8` + `fastapi-m8` stack, copy
`env.example` to `.env` and change configuration only:

1. **Auth URL** — `LIVE_TEST_AUTH_BASE` → your issuer's public base.
2. **Service URL(s)** — `LIVE_TEST_SVC_BASE` / `LIVE_TEST_SVC_BASES` +
   `LIVE_TEST_DEFAULT_SVC` → your `fastapi-m8` consumers (each service's
   `API_PREFIX` root). For media-service this is `/media`.
3. **Protected endpoints** — `LIVE_TEST_PROTECTED_ENDPOINTS` → the real
   authenticated read endpoints of each service, keyed by service name.
4. **Public entrypoint / TLS** — `LIVE_TEST_PUBLIC_BASE`, and
   `LIVE_TEST_PUBLIC_TLS_VERIFY=false` (or a CA bundle path) for self-signed
   local certs; leave verification on for a real CA.
5. **Roots** — `LIVE_TEST_DEPLOYMENT_ROOT` → the target compose directory;
   `LIVE_TEST_REPO_ROOT` → the stack holding the committed JWT keys (only needed
   for the asymmetric key-leak checks).

Algorithm-, token-mode-, and component-specific suites skip automatically when
they do not match the detected stack.
