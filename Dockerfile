# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN corepack enable && corepack pnpm install --frozen-lockfile
COPY frontend/ ./
RUN corepack pnpm build

FROM python:3.11-slim-bookworm AS python-builder
ENV VIRTUAL_ENV=/opt/venv \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
RUN pip install "uv==0.12.11"
RUN python -m venv "$VIRTUAL_ENV"
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY backend ./backend
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.11-slim-bookworm AS runtime
ARG VERSION=0.1.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="PackBreaker" \
      org.opencontainers.image.description="PT 自动拆包辅种系统" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.created="$BUILD_DATE" \
      org.opencontainers.image.source="https://github.com/YYxiaoma/PackBreaker" \
      org.opencontainers.image.licenses="MIT"

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PACKBREAKER_HOST=0.0.0.0 \
    PACKBREAKER_PORT=8000 \
    PACKBREAKER_CONFIG_DIR=/config \
    PACKBREAKER_DATA_DIR=/data \
    PACKBREAKER_FRONTEND_DIR=/app/frontend/dist

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 packbreaker \
    && useradd --uid 1000 --gid packbreaker --home-dir /app --create-home --shell /usr/sbin/nologin packbreaker \
    && install -d -o packbreaker -g packbreaker -m 0700 /config \
    && install -d -o packbreaker -g packbreaker -m 0750 /data

WORKDIR /app
COPY --from=python-builder /opt/venv /opt/venv
COPY --chown=packbreaker:packbreaker backend ./backend
COPY --from=frontend-builder --chown=packbreaker:packbreaker /build/frontend/dist ./frontend/dist
COPY --chown=packbreaker:packbreaker LICENSE README.md ./

USER packbreaker
EXPOSE 8000
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "backend.app.healthcheck"]
CMD ["python", "-m", "backend.app.container_entrypoint"]
