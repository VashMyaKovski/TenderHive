# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY tender_eval ./tender_eval
COPY datasets ./datasets
COPY fixtures ./fixtures

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY tests ./tests
CMD ["uv", "run", "--locked", "--no-sync", "pytest"]

FROM python:3.14.10-slim-trixie AS runtime
WORKDIR /app
RUN install -d -o 10001 -g 10001 /reports
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/tender_eval ./tender_eval
COPY --from=build /app/datasets ./datasets
COPY --from=build /app/fixtures ./fixtures
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "tender_eval"]
