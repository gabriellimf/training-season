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
# Build constraints are generated from build-requirements.in.
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

# The arch comes from dpkg, not BuildKit TARGETARCH: compose on this host has no
# buildx. The slim image has no curl, so python downloads the binary.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sha256=cc232108374e8e2b537045f2d5eaa1229f619db314c3ba6a14d38b35dc0aa123 ;; \
        arm64) sha256=4ae22576a78223d8ccdf5f3b3660a5cbb5288416bf0ca96afc4a36d195932b94 ;; \
        *) echo "supercronic: unsupported architecture '$arch', expected amd64 or arm64" >&2; exit 1 ;; \
    esac; \
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
        "https://github.com/aptible/supercronic/releases/download/v0.2.51/supercronic-linux-$arch" \
        /usr/local/bin/supercronic; \
    echo "$sha256  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/supercronic

RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot

# Root-owned, so nonroot cannot change the schedule. Comes from the builder's COPY . /app.
COPY --from=builder /app /app
RUN supercronic -test /app/deploy/crontab

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /data && chown nonroot:nonroot /data
VOLUME ["/data"]

USER nonroot
WORKDIR /app

CMD ["python", "-m", "training_season.database"]
