FROM python:3.14-slim

# ビルド依存だけを一時的に入れ、同じレイヤで消す
# Build dependencies are installed and removed in the same layer.
WORKDIR /app

COPY pyproject.toml README.md ./
COPY aipmo ./aipmo

# web は fastapi/uvicorn のみで軽量なので常に含める。1つのイメージを
# scheduler・aipmo・web の全コンテナで使い回し、WebUI を実際に起動するか
# どうかは docker-compose.yml の web サービス（--profile web）側で選ぶ。
#
# web is lightweight (just fastapi/uvicorn), so it is always included. The
# same image is shared by the scheduler, aipmo, and web containers; whether
# the WebUI actually runs is decided by docker-compose.yml's web service
# (--profile web), not by what's installed here.
RUN pip install --no-cache-dir ".[cloud,data,web]"

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
