# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY services/th-kb/pyproject.toml services/th-kb/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY services/th-kb/th_kb ./th_kb
COPY services/th-kb/migrations ./migrations

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY services/th-kb/tests ./tests
CMD ["uv", "run", "--locked", "--no-sync", "pytest"]

FROM python:3.14.10-slim-trixie AS runtime
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/th_kb ./th_kb
COPY --from=build /app/migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "th_kb"]
