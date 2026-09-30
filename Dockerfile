# Sentinel, hosted. One container runs the dashboard AND the schedule
# (`sentinel serve`); state lives on a volume mounted at /data.
#
#   fly launch --no-deploy        # once; reads fly.toml
#   fly volumes create sentinel_data --size 1
#   fly secrets set SENTINEL_DASHBOARD_PASSWORD=... ANTHROPIC_API_KEY=... \
#       EODHD_API_KEY=... FINNHUB_API_KEY=...
#   fly deploy
#
# See deploy/README.md -> "Hosted on Fly.io".
FROM python:3.12-slim AS build

ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first, so a code-only change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra llm --extra dashboard
COPY src ./src
COPY streamlit_app.py ./
RUN uv sync --frozen --no-dev --extra llm --extra dashboard


FROM python:3.12-slim

# tzdata: the schedule is wall-clock in Europe/London (DST-correct); slim images have none.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir /data

COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SENTINEL_HOME=/data \
    PORT=8080

# Runs as root on purpose: a Fly volume is mounted root-owned at start-up, so a non-root
# USER could not create the database on first boot. The machine is an isolated VM with
# one process of ours in it; the password and the secrets store are the boundary.
WORKDIR /data
VOLUME /data
EXPOSE 8080

# Health is Streamlit's own endpoint; the platform restarts the machine if it fails.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/_stcore/health', timeout=4)"

CMD ["sentinel", "serve"]
