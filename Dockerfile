# ---- frontend build ---------------------------------------------------------
FROM node:24-slim AS frontend
WORKDIR /app/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

# ---- backend + static frontend -------------------------------------------
FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    FRONTEND_DIST=/app/frontend/dist
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=frontend /app/frontend/dist /app/frontend/dist
EXPOSE 8000
CMD ["sh", "-c", "alembic -c src/accrueboard/db/alembic.ini upgrade head && uvicorn accrueboard.api.app:create_app --factory --host 0.0.0.0 --port 8000"]
