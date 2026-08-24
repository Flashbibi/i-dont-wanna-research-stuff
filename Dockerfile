# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /opt/beschaffung

RUN useradd --create-home --uid 10001 beschaffung

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Nur was der Dienst zur Laufzeit liest. Tests und Historie bleiben draussen.
# extension/ gehört dazu: /extension.zip zippt dieses Verzeichnis im Moment
# des Abrufs, und ohne es fällt die Job-Seite auf den Kopierflow zurück.
# LICENSE reist mit, weil die AGPL das Bereitstellen des Angebots verlangt.
# adapters/ gehört dazu: die Registry liest gebündelte Adapter von dort, und
# ohne das Verzeichnis hätte das Image keinen einzigen.
COPY app/ ./app/
COPY adapters/ ./adapters/
COPY migrations/ ./migrations/
COPY static/ ./static/
COPY templates/ ./templates/
COPY extension/ ./extension/
COPY LICENSE ./

USER beschaffung

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Migrationen laufen in app.__main__ beim Start, deshalb kein eigener Entrypoint.
CMD ["python", "-m", "app"]
