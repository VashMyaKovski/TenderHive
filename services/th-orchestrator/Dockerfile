# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY services/th-orchestrator/pyproject.toml services/th-orchestrator/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY contracts/__init__.py contracts/agent_task.py contracts/models.py contracts/risk_task_v1.py ./contracts/
COPY services/th-orchestrator/tender_orchestrator ./tender_orchestrator

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY services/th-orchestrator/tests ./tests
COPY services/th-agents/examples/minsk-supply-risk.json ./examples/minsk-supply-risk.json
CMD ["uv", "run", "--locked", "--no-sync", "pytest"]

FROM python:3.14.10-slim-trixie AS runtime
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/contracts ./contracts
COPY --from=build /app/tender_orchestrator ./tender_orchestrator
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "tender_orchestrator"]
