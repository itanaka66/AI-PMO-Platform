# どのクラウドの x86_64 / arm64 でも動く / works on x86_64 or arm64, any cloud.
# python:3.12-slim はマルチアーキテクチャ対応なので、書き換え不要。
# python:3.12-slim is a multi-arch image, so this needs no per-provider change.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY aipmo ./aipmo
# ローカル LLM を積まない構成なので、data と web と cloud のみ。
# No local-LLM stack here, so only the data, web and cloud extras.
RUN pip install --no-cache-dir ".[cloud,data,web]"

COPY sql ./sql
COPY queries.yaml ./

RUN useradd --create-home --uid 10001 aipmo && chown -R aipmo:aipmo /app
USER aipmo

ENTRYPOINT ["python", "-m", "aipmo.cli"]
CMD ["--help"]
