# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY requirements/build.lock /build/requirements/build.lock
COPY requirements/service.lock /build/requirements/service.lock
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --requirement /build/requirements/build.lock

COPY pyproject.toml README.md LICENSE /build/
COPY src /build/src
RUN python -m pip wheel \
      --no-deps \
      --no-build-isolation \
      --wheel-dir /tmp/wheels \
      /build
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install \
      --requirement /build/requirements/service.lock \
    && /opt/venv/bin/python -m pip install --no-deps /tmp/wheels/*.whl \
    && /opt/venv/bin/python -m pip check

FROM ${PYTHON_IMAGE} AS runtime

ARG SOURCE_REVISION=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Quant Backtester" \
      org.opencontainers.image.description="Deterministic backtesting control plane and worker" \
      org.opencontainers.image.version="2.0.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    QBT_SOURCE_REVISION="${SOURCE_REVISION}" \
    QBT_ENVIRONMENT=production

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/runs \
    && chown -R 10001:10001 /app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]

CMD ["quant-backtester-api"]
