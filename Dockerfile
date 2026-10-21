FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    KEY_FILE=/keys/secret.key \
    WEB_PORT=8888

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py web.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME /data /keys
EXPOSE 8888
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
