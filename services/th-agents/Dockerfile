# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY services/th-agents/pyproject.toml services/th-agents/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY services/th-agents/tender_agents ./tender_agents
COPY contracts/__init__.py contracts/risk_task_v1.py ./contracts/
COPY services/th-agents/examples ./examples

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY services/th-agents/tests ./tests
CMD ["uv", "run", "--locked", "--no-sync", "pytest"]

FROM python:3.14.10-slim-trixie AS runtime
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/tender_agents ./tender_agents
COPY --from=build /app/contracts ./contracts
COPY --from=build /app/examples ./examples
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "tender_agents"]
