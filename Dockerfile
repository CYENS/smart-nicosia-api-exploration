# Smart Nicosia collector: polls the city API into DuckDB in a loop.
#   docker build -t smart-nicosia-collector .
#   docker run -d --name collector --restart unless-stopped -v "$PWD/data:/app/data" smart-nicosia-collector
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY smart_nicosia_api.py ./
COPY collector ./collector
RUN uv sync --frozen --no-dev

# The DuckDB file, the log and data/camera_locations.csv live here.
VOLUME /app/data
CMD ["uv", "run", "--no-sync", "collector", "run"]
