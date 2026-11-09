FROM python:3.12-slim

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY call_stats_bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY call_stats_bot ./call_stats_bot

EXPOSE 8000
CMD ["uvicorn", "call_stats_bot.main:app", "--host", "0.0.0.0", "--port", "8000"]

