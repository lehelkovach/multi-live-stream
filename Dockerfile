# syntax=docker/dockerfile:1.7
# Single image for the Python services (api, relay, chat). Multi-arch: linux/arm64 + amd64.

FROM python:3.12-slim-bookworm AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install --no-cache-dir "uv==0.11.32"
WORKDIR /src
# Dependencies first (cached unless the lockfile changes)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
# Then the project itself
COPY core ./core
COPY api ./api
COPY relay ./relay
COPY chat ./chat
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 omni \
    && mkdir -p /data && chown omni:omni /data
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OMNI_WEB_DIST_DIR=/nonexistent
WORKDIR /app
USER omni
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
