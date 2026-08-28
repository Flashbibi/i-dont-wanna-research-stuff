"""Update-Check und Banner - nie gegen echtes Netz."""

import pytest
from fastapi.testclient import TestClient

from app import updates
from app.web import create_app

RELEASE_LINK = "https://github.com/Flashbibi/i-dont-wanna-research-stuff/releases/tag/"


class LeeresRepository:
    def list_jobs(self, limit=20):
        return []


@pytest.fixture(autouse=True)
def check_eingeschaltet(monkeypatch):
    """Diese Datei prüft den Check, also läuft er hier - anders als sonst."""
    monkeypatch.setenv("BESCHAFFUNG_UPDATE_CHECK", "on")


def seite():
    return TestClient(create_app(LeeresRepository(), lambda: 16))


def netz(monkeypatch, payload=None, fehler=None):
    """Den einzigen Netzzugriff ersetzen und seine Aufrufe mitzählen."""
    aufrufe = []

    def _fetch():
        aufrufe.append(1)
        if fehler is not None:
            raise fehler
        return payload

    monkeypatch.setattr(updates, "_fetch", _fetch)
    return aufrufe


def test_a_newer_release_shows_a_banner_that_links_to_it(monkeypatch):
    netz(
        monkeypatch, {"tag_name": "v9.9.9", "html_url": "https://example.invalid/fremd"}
    )

    seiteninhalt = seite().get("/").text

    assert "Update verfügbar: v9.9.9" in seiteninhalt
    assert f'href="{RELEASE_LINK}v9.9.9"' in seiteninhalt
    # Der Link entsteht aus dem geprüften Tag, nicht aus der Antwort.
    assert "example.invalid" not in seiteninhalt


@pytest.mark.parametrize("tag", ["v0.1.0", "v0.0.9"])
def test_the_same_or_an_older_release_stays_quiet(monkeypatch, tag):
    netz(monkeypatch, {"tag_name": tag})

    antwort = seite().get("/")

    assert antwort.status_code == 200
    assert "Update verfügbar" not in antwort.text


@pytest.mark.parametrize(
    "payload", [{"tag_name": "release-9"}, {"tag_name": None}, {}, []]
)
def test_an_answer_without_a_usable_tag_is_no_banner(monkeypatch, payload):
    netz(monkeypatch, payload)

    antwort = seite().get("/")

    assert antwort.status_code == 200
    assert "Update verfügbar" not in antwort.text


def test_a_failing_check_still_renders_the_page(monkeypatch):
    netz(monkeypatch, fehler=OSError("kein Netz"))

    antwort = seite().get("/")

    assert antwort.status_code == 200
    assert "Update verfügbar" not in antwort.text


def test_switching_the_check_off_keeps_it_off_the_network(monkeypatch):
    monkeypatch.setenv("BESCHAFFUNG_UPDATE_CHECK", "off")
    aufrufe = netz(monkeypatch, {"tag_name": "v9.9.9"})

    antwort = seite().get("/")

    assert antwort.status_code == 200
    assert "Update verfügbar" not in antwort.text
    assert aufrufe == []


def test_the_cache_keeps_the_second_page_off_the_network(monkeypatch):
    aufrufe = netz(monkeypatch, {"tag_name": "v9.9.9"})
    client = seite()

    erste = client.get("/")
    zweite = client.get("/")

    assert "Update verfügbar: v9.9.9" in erste.text
    assert "Update verfügbar: v9.9.9" in zweite.text
    assert aufrufe == [1]
