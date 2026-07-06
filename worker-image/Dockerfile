# syntax=docker/dockerfile:1.7
#
# PymoniK v2 worker image. Baked-cloudpickle mode: the image ships with the
# pymonik package installed; user tasks arrive as cloudpickle blobs inside a
# msgspec envelope and are dispatched by pymonik.worker.run().
#
# Build (from the repo root):
#
#     docker build -t pymonik-worker:<tag> -f worker-image/Dockerfile .
#
# CI publishes this image automatically on release; see
# .github/workflows/publish-images.yml.

ARG PYTHON_VERSION=3.11
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim

# ArmoniK convention: workers run as `armonikuser` (uid/gid 5000) with
# /cache owned by that user. The agent <-> worker socket lives under /cache.
RUN groupadd --gid 5000 armonikuser \
 && useradd --home-dir /home/armonikuser --create-home --uid 5000 --gid 5000 \
            --shell /bin/sh --skel /dev/null armonikuser \
 && mkdir /cache \
 && chown armonikuser: /cache

WORKDIR /app
RUN chown -R armonikuser: /app
USER armonikuser

# Install the pymonik package into /app/.venv.
COPY --chown=armonikuser:armonikuser pyproject.toml README.md ./
COPY --chown=armonikuser:armonikuser src/ ./src/

# uv-dynamic-versioning derives the version from git tags, but the build
# context carries no .git and this base image has no git binary. Bypass
# version detection with an explicit string. Stamp a real version at build
# time with `--build-arg PYMONIK_VERSION=$(git describe --tags --always)`;
# the default keeps the image importable when the tag doesn't matter.
ARG PYMONIK_VERSION=0.0.0
ENV UV_DYNAMIC_VERSIONING_BYPASS=${PYMONIK_VERSION}

RUN uv venv /app/.venv \
 && uv pip install --no-cache '.[otel]'

ENV PATH="/app/.venv/bin:${PATH}"
ENV PYTHONUNBUFFERED=1

# The console script pymonik-worker wraps pymonik.worker.run(), which uses
# armonik.worker.armonik_worker() to bind to the agent socket under /cache.
ENTRYPOINT ["pymonik-worker"]
