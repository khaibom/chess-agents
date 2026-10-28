FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS build
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.23
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
RUN apt-get update && apt-get install -y --no-install-recommends stockfish=15.1-4 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chess && useradd --uid 10001 --gid chess --create-home chess \
    && mkdir /data && chown chess:chess /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY LICENSE THIRD_PARTY_NOTICES.md /app/
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CHESS_DB=/data/chess-agents.sqlite3 STOCKFISH_PATH=/usr/games/stockfish
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["chess-agents", "bot"]
