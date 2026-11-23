# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY services/th-integrations/pyproject.toml services/th-integrations/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY contracts/__init__.py contracts/models.py ./contracts/
COPY services/th-integrations/tender_integrations ./tender_integrations

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY services/th-integrations/tests ./tests
CMD ["uv", "run", "--locked", "--no-sync", "pytest"]

FROM python:3.14.10-slim-trixie AS runtime
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/contracts ./contracts
COPY --from=build /app/tender_integrations ./tender_integrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "tender_integrations"]
