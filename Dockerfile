# Research-only fictional demo image for the v2 review API and reviewer UI.
#
# It suggests POSSIBLE reference-record matches for HUMAN review; it never
# establishes identity, wrongdoing, sanctions status or clearance. It ships the
# packaged fictional entities only: no source snapshots, review stores, models,
# keys or data/ content (see .dockerignore).
#
# Status: written in Completion Step 29 but NOT built or run there (building
# needs network access to the base images and PyPI, which was not approved).
# Base images are pinned by tag only; pinning them by digest is an open item.
#
# Build:  docker build -t polyglot-watchlist:dev .      (or: podman build ...)
# Run:    see docker-compose.yml and docs/reproduction.md.

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.14-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
# Dependencies first (locked core + agent + api + ui extras; no torch, no dev
# or QA tools), then the project itself as a regular, non-editable install.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project \
        --extra agent --extra api --extra ui --python /usr/local/bin/python3
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
        --extra agent --extra api --extra ui --python /usr/local/bin/python3

FROM python:3.14-slim
RUN groupadd --system --gid 10001 watchlist \
    && useradd --system --uid 10001 --gid watchlist --home-dir /home/watchlist \
        --create-home --shell /usr/sbin/nologin watchlist \
    && mkdir -p /workspace \
    && chown watchlist:watchlist /workspace
COPY --from=build /opt/venv /opt/venv
COPY config/review-v2.json config/memory.json /app/config/
# Offline at runtime: no model hub, tracing or Streamlit usage statistics.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_HEADLESS=true \
    WATCHLIST_LOG_FORMAT=json \
    HOME=/home/watchlist
WORKDIR /app
USER 10001:10001
# Case ledgers, checkpoints and (optionally) the memory store live here only.
VOLUME ["/workspace"]
# The API binds loopback inside the container (its CLI refuses other hosts);
# only the UI port is meant to be published, to the host's loopback.
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-B", "-c", "import sys, urllib.request as u; r = u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"]
# Keys come from WATCHLIST_API_KEYS_FILE (a mounted secret), never the image.
CMD ["python", "-B", "-m", "watchlist.api", "serve", \
     "--workspace", "/workspace/review", "--config", "/app/config/review-v2.json", \
     "--host", "127.0.0.1", "--port", "8000"]
