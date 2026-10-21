# Nicosia digital twin chatbot. Built by compose.yaml (next to Caddy); see deploy/README.md.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY smart_nicosia_api.py ./
COPY collector ./collector
COPY chatbot ./chatbot
COPY data/camera_locations.csv data/eoa_registers_seed.json ./data/
RUN uv sync --frozen --no-dev

# The caches (3D tiles, registers, places index) go in a volume, owned by an unprivileged user.
RUN useradd --system chatbot \
    && mkdir -p data/chatbot_cache && chown chatbot data/chatbot_cache
USER chatbot
VOLUME /app/data/chatbot_cache

# Listen on all interfaces inside the container; only Caddy can reach it.
ENV CHATBOT_HOST=0.0.0.0 CHATBOT_PORT=8000
EXPOSE 8000
CMD ["/app/.venv/bin/chatbot"]
