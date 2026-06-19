FROM python:3.11-slim

# ffmpeg — для декодирования OGG/Opus голосовых из Telegram
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Заранее тянем Whisper-модель в образ, чтобы не качать при первом голосовом
ENV WHISPER_MODEL=small
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"

COPY . .
CMD ["python", "bot.py"]
