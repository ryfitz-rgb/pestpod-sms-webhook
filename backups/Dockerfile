# Render Cron Job image for the nightly pestpod-db backup.
# Debian trixie ships PostgreSQL 17 client tools, matching the pestpod-db server (17).
FROM python:3.13-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pg_dump --version | grep -q "PostgreSQL) 17\."

WORKDIR /app
COPY requirements.txt /app/backups/requirements.txt
RUN pip install --no-cache-dir -r /app/backups/requirements.txt
COPY . /app/backups/

ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "backups.pg_backup"]
