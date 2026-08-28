"""Der Image-Pin im Compose-File muss die laufende Version nennen."""

from pathlib import Path

from app.version import __version__

IMAGE = "ghcr.io/flashbibi/i-dont-wanna-research-stuff:"


def test_compose_pins_the_image_to_the_running_version():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    zeilen = [zeile.strip() for zeile in compose.splitlines() if IMAGE in zeile]

    assert len(zeilen) == 1, f"Genau eine Image-Zeile erwartet, gefunden: {zeilen}"
    assert zeilen[0].endswith(f":v{__version__}"), zeilen[0]
