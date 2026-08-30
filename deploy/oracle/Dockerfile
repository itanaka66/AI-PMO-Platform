# arm64 向け / built for arm64 (Ampere A1).
FROM python:3.14-slim

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
