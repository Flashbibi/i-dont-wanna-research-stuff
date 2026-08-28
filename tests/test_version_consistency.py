# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Der Image-Pin im Compose-File muss die laufende Version nennen.

Gepinnt bleibt gepinnt: ``docker compose pull`` soll niemanden zufällig auf
einen neuen Stand heben - wann aktualisiert wird, entscheidet der Mensch, und
dass es etwas Neues gibt, sagt der Update-Banner. Genau deshalb altert der Pin
still, wenn ihn beim Versionsschnitt niemand ansieht: nach 0.2.0 zeigte er noch
auf 0.1.0, und die Schnellstartanleitung versprach damit einen Stand, den sie
nicht liefert.

Dieser Test ist derselbe Stolperdraht wie das Versionsliteral im Footer-Test:
die Version kann sich nicht bewegen, ohne dass jemand den Pin anfasst.
"""
from pathlib import Path

from app.version import __version__

IMAGE = "ghcr.io/flashbibi/i-dont-wanna-research-stuff:"


def test_compose_pins_the_image_to_the_running_version():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    zeilen = [zeile.strip() for zeile in compose.splitlines() if IMAGE in zeile]

    assert len(zeilen) == 1, f"Genau eine Image-Zeile erwartet, gefunden: {zeilen}"
    assert zeilen[0].endswith(f":v{__version__}"), zeilen[0]
