# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build
WORKDIR /app
ENV UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY services/th-solver/pyproject.toml services/th-solver/uv.lock ./
RUN uv sync --locked --no-dev
COPY contracts/__init__.py contracts/docintel_v1.py ./contracts/
COPY services/th-solver/tender_solver ./tender_solver

FROM build AS test
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && uv sync --locked
COPY contracts/models.py ./contracts/
COPY services/th-solver/tests ./tests
COPY tests/test_contracts.py tests/test_docintel_contract.py ./contract_tests/
CMD ["sh", "-ec", "uv run --locked --no-sync ruff check . && uv run --locked --no-sync ruff format --check . && uv run --locked --no-sync pyright && uv run --locked --no-sync pytest tests contract_tests"]

FROM python:3.14.10-slim-trixie AS runtime
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/contracts ./contracts
COPY --from=build /app/tender_solver ./tender_solver
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "tender_solver"]
