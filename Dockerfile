# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /opt/beschaffung

RUN useradd --create-home --uid 10001 beschaffung

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Nur was der Dienst zur Laufzeit liest. Tests, Extension und Historie bleiben
# draussen; was fehlt, kann auch nicht ungeprüft mitlaufen.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY static/ ./static/
COPY templates/ ./templates/

USER beschaffung

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Migrationen laufen in app.__main__ beim Start, deshalb kein eigener Entrypoint.
CMD ["python", "-m", "app"]
