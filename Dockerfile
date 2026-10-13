FROM python:3.12-slim
WORKDIR /srv/redsaves
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY RedSaves2.0.py ./
# /data holds the SQLite DB, run log, and (on first boot) the migrated
# sent_links.txt. Mount it as a volume so history survives redeploys.
VOLUME /data
ENV REDSAVES_DATA_DIR=/data
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD curl -f http://localhost:8000/api/health || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
