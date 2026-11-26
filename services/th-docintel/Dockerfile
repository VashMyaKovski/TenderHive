FROM ghcr.io/astral-sh/uv:0.13.8-python3.14-trixie-slim AS build

WORKDIR /app
ENV UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
ENV UV_HTTP_TIMEOUT=120 UV_HTTP_RETRIES=10
COPY services/th-docintel/pyproject.toml services/th-docintel/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
COPY contracts ./contracts
COPY services/th-docintel/tender_docintel ./tender_docintel

FROM build AS test
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked
COPY services/th-docintel/tests ./tests
CMD ["sh", "-ec", "uv run --locked --no-sync ruff check . && uv run --locked --no-sync ruff format --check . && uv run --locked --no-sync pyright && uv run --locked --no-sync pytest"]

FROM python:3.14.10-slim-trixie AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-rus \
        passwd \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home docintel

WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/contracts ./contracts
COPY --from=build /app/tender_docintel ./tender_docintel
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["python", "-m", "tender_docintel"]
