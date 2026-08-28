"""Gemeinsame Absicherung für die ganze Suite."""

import pytest

from app import updates


@pytest.fixture(autouse=True)
def kein_stiller_update_check(monkeypatch):
    """Der Update-Check hängt im Aufbau jeder Seite.

    Ohne diese Sperre würde die erste gerenderte Seite der Suite wirklich die
    GitHub-API befragen - nur einmal pro Prozess, dank Cache, aber eben echt.
    Der Cache fällt hier gleich mit, sonst färbt ein Test den nächsten ein.
    Wer das Banner prüft, schaltet den Check selbst wieder an.
    """
    monkeypatch.setenv("BESCHAFFUNG_UPDATE_CHECK", "off")
    monkeypatch.setattr(updates, "_cache", None)
