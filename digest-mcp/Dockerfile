FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY digest ./digest

# Данные (state.json + snapshots.jsonl) — в томе, не в образе.
ENV NEXUS_DIGEST_DATA_DIR=/data
VOLUME ["/data"]

# Порт слушаем только внутри compose-сети: наружу он не публикуется (см. RFC §5).
EXPOSE 8080

CMD ["python", "-m", "digest.server"]
