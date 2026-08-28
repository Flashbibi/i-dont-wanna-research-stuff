"""Gemeinsame Absicherung für die ganze Suite."""

import pytest

from app import updates


@pytest.fixture(autouse=True)
def kein_stiller_update_check(monkeypatch):
    """Ohne diese Sperre befragte die erste gerenderte Seite der Suite wirklich die
    GitHub-API; der Cache fällt mit, sonst färbt ein Test den nächsten ein."""
    monkeypatch.setenv("BESCHAFFUNG_UPDATE_CHECK", "off")
    monkeypatch.setattr(updates, "_cache", None)
