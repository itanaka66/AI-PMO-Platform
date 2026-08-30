FROM python:3.14-slim

# ビルド依存だけを一時的に入れ、同じレイヤで消す
# Build dependencies are installed and removed in the same layer.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY aipmo ./aipmo

RUN pip install --no-cache-dir ".[cloud,data]"

COPY prompts ./prompts
COPY templates ./templates
COPY sql ./sql
COPY queries.yaml config.docker.yaml ./

# root で動かさない / do not run as root
RUN useradd --create-home --uid 10001 aipmo && chown -R aipmo:aipmo /app
USER aipmo

ENV AIPMO_CONFIG=/app/config.docker.yaml
ENTRYPOINT ["aipmo"]
CMD ["--help"]
