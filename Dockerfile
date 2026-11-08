FROM python:3.12-slim

LABEL org.opencontainers.image.title="Crafty Medic" \
      org.opencontainers.image.description="Diagnosi e riparazione one-click dei server Minecraft gestiti da Crafty Controller" \
      org.opencontainers.image.source="https://github.com/AleCl1210/FixErrorsInCraftyController"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CRAFTY_MEDIC_DATA_DIR=/data \
    CRAFTY_MEDIC_HOST=0.0.0.0 \
    CRAFTY_MEDIC_PORT=8765

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 medic \
    && useradd -u 1000 -g 1000 -m -d /home/medic -s /bin/bash medic

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /data && chown -R medic:medic /data /app

EXPOSE 8765

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=5).status==200 else 1)"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app"]
