# ---------- BUILD STAGE ----------
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2 AS builder

WORKDIR /install

ARG ENVIRONMENT=prod
ENV PIP_ROOT_USER_ACTION=ignore

COPY ./media_service/requirements_base.txt .
COPY ./media_service/requirements_dev.txt .
COPY ./media_service/requirements_prod.txt .
COPY ./media_service/requirements_prod.lock .

# Release images install from a hash-locked, fully pinned dependency set so a
# rebuild of the same source cannot silently resolve a different graph
# (finding 11.8). `--require-hashes` is enforced for every non-development
# build. Regenerate the lock with:
#   pip-compile --generate-hashes --no-emit-index-url \
#     --output-file=requirements_prod.lock requirements_prod.txt
RUN pip install --upgrade "pip==26.1.1" && \
    if [ "$ENVIRONMENT" = "development" ]; then \
        pip install --prefix=/install --no-cache-dir -r requirements_base.txt && \
        pip install --prefix=/install --no-cache-dir -r requirements_dev.txt ; \
    else \
        pip install --prefix=/install --no-cache-dir \
            --require-hashes -r requirements_prod.lock ; \
    fi


# ---------- RUNTIME STAGE ----------
FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/home/appuser/.local/bin:$PATH"
ENV PYTHONPATH=/opt:/opt/media_service

# Debian patch layer — the one fleet form; owner: the workspace's
# `.workspace/context/debian-patch-layer.md`. Every Debian package self-heals
# on each build through `apt-get upgrade -y`; nothing is exact-pinned and
# nothing is installed that the base does not already ship. The base digest
# above moves in lock-step with the other service images, never alone. If a
# package must ever lead the pinned base, the recorded form is an unpinned
# install plus a `dpkg --compare-versions … ge <floor>` assertion in this same
# RUN — never an exact `=` pin, which freezes the image on the next advisory.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --create-home appuser

WORKDIR /opt

COPY --from=builder /install /usr/local

# Strip the installer tooling from the runtime image. The entrypoint is
# `docker_start.sh` -> alembic + uvicorn; this image is built from a hash-locked
# set and never installs anything at run time, so pip, setuptools and wheel are
# pure attack surface. Mirrors media-worker-m8.
#
# At the digest pinned above the base ships pip alone — setuptools and wheel are
# already absent and their uninstall reports "Skipping" — so this layer removes
# pip today and stands as a guard for the rest: a later base bump that
# reintroduces setuptools cannot silently add its CVEs (e.g. CVE-2025-47273 in
# 70.3.0) to the image.
#
# Ordered after the COPY above so the builder's tree cannot reintroduce them,
# and pip uninstalls itself last because the earlier uninstalls need it.
# The two checks below are the guard, and fail the build if the removal broke
# anything: the import line covers the declared runtime graph (it fails if any
# dependency needed `pkg_resources`/`setuptools` at import time), and the
# console-script line covers the entrypoint's own binaries.
RUN python -m pip uninstall -y setuptools wheel && \
    python -m pip uninstall -y pip && \
    python -c "import alembic, arq, asyncpg, fastapi, fastapi_m8, filetype, httpx, jinja2, media_sdk_m8, boto3, botocore, prometheus_client, pydantic, pydantic_settings, redis, slugify, sqlalchemy, sqlmodel, starlette, tenacity, uvicorn" && \
    alembic --version && uvicorn --version && gunicorn --version

COPY --chown=appuser:appuser ./media_service ./media_service

RUN find /opt/media_service/scripts -name "*.sh" -exec sed -i 's/\r$//' {} + && \
    chmod +x /opt/media_service/scripts/*.sh

USER appuser

EXPOSE 8000

CMD ["./media_service/scripts/docker_start.sh"]
