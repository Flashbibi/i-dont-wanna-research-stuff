FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /opt/beschaffung

RUN useradd --create-home --uid 10001 beschaffung

# Nur was der Dienst zur Laufzeit liest; Tests und Historie bleiben draussen.
COPY pyproject.toml LICENSE ./
COPY app/ ./app/
RUN pip install --no-cache-dir .
COPY adapters/ ./adapters/
COPY migrations/ ./migrations/
COPY static/ ./static/
COPY templates/ ./templates/
COPY extension/ ./extension/

USER beschaffung

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]

# Migrationen laufen in app.__main__ beim Start, deshalb kein eigener Entrypoint.
CMD ["python", "-m", "app"]
