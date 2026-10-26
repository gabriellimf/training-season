# Multi-stage image: build with uv, run on python:3.12-slim-trixie.
# No BuildKit mounts. docker compose on this host has no buildx plugin.
# Pattern from https://docs.astral.sh/uv/guides/integration/docker/ (uv 0.11).

FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
ENV UV_NO_DEV=1
ENV UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-install-project
COPY . /app
RUN uv build --wheel --build-constraints build-requirements.txt --require-hashes \
    && uv pip install --python /app/.venv/bin/python --no-deps /app/dist/*.whl


FROM python:3.12-slim-trixie

ENV TZ=America/Sao_Paulo
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown nonroot:nonroot /data
VOLUME ["/data"]

USER nonroot
WORKDIR /app

CMD ["python", "-c", "from training_season import __version__; print(__version__)"]
