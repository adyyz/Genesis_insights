ARG NODE_IMAGE=node:22.7.0-bookworm-slim@sha256:1289f8e0bf3a182990d1d046f8ab2a5d45814f8b40b41963650a42c30c91f39e
ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336

FROM ${NODE_IMAGE} AS frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json vite.config.ts ./
COPY frontend ./frontend
RUN npm run build

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-python-downloads

FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_MODE=cloud LIVE_AI_ENABLED=false CREDITS_VERIFIED=false LOCAL_GEMINI_ENABLED=false
COPY --from=dependencies --chown=65532:65532 /app/.venv /app/.venv
COPY --chown=65532:65532 backend ./backend
RUN python -m backend.bulk_dataset --output /app/generated/bulk > /dev/null
ENV GENESIS_BULK_DIR=/app/generated/bulk
USER 65532:65532

# One-shot emulator worker only. This is not the Cloud Tasks HTTP worker.
FROM runtime AS local-worker
ENV APP_MODE=local
CMD ["python", "-m", "backend.jobs", "--once"]

FROM runtime AS worker
EXPOSE 8080
CMD ["python", "-m", "backend.container", "--worker"]

FROM runtime AS web
COPY --from=frontend --chown=65532:65532 /app/dist ./dist
EXPOSE 8080
CMD ["python", "-m", "backend.container"]
