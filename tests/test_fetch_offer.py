# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Die Engine liest, die Engine schreibt - und sonst niemand.

Der Weg von der Produkt-URL zum Angebot, komplett gegen Attrappen: HTTP läuft
über ``httpx.MockTransport``, die Seite ist die synthetische Fixture, und
geschlafen wird nie. Was hier zählt, ist die Reihenfolge der Prüfungen und dass
im Fehlerfall **nichts** geschrieben wird.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app import adapter, fetch
from app.procurement import ProcurementService, ValidationError


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters" / "demo"
SEITE = (FIXTURES / "produkt.html").read_text(encoding="utf-8")

PRODUKT_URL = "https://demoshop.example/produkt/mg996r"
ROBOTS_ALLES = "User-agent: *\nAllow: /\n"
ROBOTS_VERBIETET = "User-agent: *\nDisallow: /produkt/\n"


class FakeRepository:
    """Nur so viel Datenbank, wie fetch_offer anfasst."""

    def __init__(self, *, status: str = "bestaetigt", waehrung: str = "CHF"):
        self.shops = {
            1: {
                "id": 1,
                "name": "Demoshop",
                "url": "https://demoshop.example",
                "domain": "demoshop.example",
                "status": status,
                "versand_waehrung": waehrung,
            }
        }
        self.lines = {10: {"id": 10, "job_id": 5, "suchtext": "Servo", "menge": 1}}
        self.offers: list[dict] = []
        self.kurse: dict[str, dict] = {}

    def get_line(self, line_id):
        return self.lines.get(line_id)

    def get_shop(self, shop_id):
        return self.shops.get(shop_id)

    def list_shops(self):
        return [dict(shop) for shop in self.shops.values()]

    def get_kurs(self, waehrung):
        return self.kurse.get(waehrung)

    def save_kurs(self, waehrung, kurs, geholt_am, quelle_url):
        row = {"waehrung": waehrung, "kurs": kurs, "geholt_am": geholt_am,
               "quelle_url": quelle_url}
        self.kurse[waehrung] = row
        return row

    def create_offer(self, **values):
        row = {"id": len(self.offers) + 20, "gesehen_am": "jetzt", **values}
        self.offers.append(row)
        return row


@pytest.fixture(autouse=True)
def registry_und_uhr(monkeypatch):
    """Demo-Adapter als einzige Registry, und kein echtes Warten."""
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", FIXTURES)
    monkeypatch.setattr(adapter, "_registry", None)
    monkeypatch.delenv(adapter.ENV_ADAPTER_DIR, raising=False)
    monkeypatch.setattr(fetch, "_letzter_request", {})
    monkeypatch.setattr(fetch, "_robots_cache", {})
    schlaefe: list[float] = []
    monkeypatch.setattr(fetch, "_schlafe", schlaefe.append)
    return schlaefe


def netz(monkeypatch, handler):
    aufrufe: list[httpx.Request] = []

    def merkend(request: httpx.Request) -> httpx.Response:
        aufrufe.append(request)
        return handler(request)

    monkeypatch.setattr(fetch, "_transport", lambda: httpx.MockTransport(merkend))
    return aufrufe


def demoshop(robots: str = ROBOTS_ALLES, seite: str = SEITE):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return httpx.Response(200, html=seite)

    return handler


def dienst(**kwargs) -> tuple[ProcurementService, FakeRepository]:
    repository = FakeRepository(**kwargs)
    return ProcurementService(repository), repository


def test_the_engine_records_the_offer_from_literal_page_text(monkeypatch, registry_und_uhr):
    netz(monkeypatch, demoshop())
    service, repository = dienst()

    ergebnis = service.fetch_offer(10, PRODUKT_URL)

    assert ergebnis["produktname"] == "MG996R Servo Metallgetriebe"
    assert ergebnis["preis_chf"] == Decimal("12.90")
    assert ergebnis["waehrung"] == "CHF"
    # Wörtlich von der Seite, nicht abgetippt.
    assert ergebnis["lieferzeit_text"] == "Lieferzeit: 2–3 Werktage"
    assert ergebnis["lager_text"] == "an Lager (5 Stück)"
    # Und durch denselben Lieferzeit-Parser wie der manuelle Weg.
    assert ergebnis["lieferzeit_tage"] == 3
    assert ergebnis["artikelnummer"] == "DEMO-4711"
    assert ergebnis["erfasst_via"] == "adapter:demo"
    assert len(repository.offers) == 1


def test_the_answer_shows_the_raw_text_per_field(monkeypatch, registry_und_uhr):
    netz(monkeypatch, demoshop())
    service, _ = dienst()

    extraktion = service.fetch_offer(10, PRODUKT_URL)["extraktion"]

    assert extraktion["adapter"] == "demo"
    assert extraktion["final_url"] == PRODUKT_URL
    assert extraktion["felder"]["preis"] == "CHF 12.90"
    assert extraktion["felder"]["lager_text"] == "an Lager (5 Stück)"


def test_the_adapter_delay_reaches_the_fetch_layer(monkeypatch, registry_und_uhr):
    netz(monkeypatch, demoshop())
    service, _ = dienst()

    service.fetch_offer(10, PRODUKT_URL)

    # demo.yaml erhöht auf 6 Sekunden; gewartet wird zwischen robots.txt und
    # Seite, abzüglich der Zeit, die der robots-Abruf selbst gebraucht hat.
    assert len(registry_und_uhr) == 1
    assert fetch.DEFAULT_MIN_DELAY_S < registry_und_uhr[0] <= 6.0


def test_an_unknown_line_never_reaches_the_network(monkeypatch, registry_und_uhr):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst()

    with pytest.raises(ValidationError, match="Zeile 999 ist unbekannt"):
        service.fetch_offer(999, PRODUKT_URL)

    assert aufrufe == []
    assert repository.offers == []


def test_without_a_known_shop_record_shop_comes_first(monkeypatch, registry_und_uhr):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst()
    repository.shops.clear()

    with pytest.raises(ValidationError, match="zuerst record_shop aufrufen"):
        service.fetch_offer(10, PRODUKT_URL)

    assert aufrufe == []
    assert repository.offers == []


def test_a_blocked_shop_is_not_fetched(monkeypatch, registry_und_uhr):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst(status="gesperrt")

    with pytest.raises(ValidationError, match="gesperrt"):
        service.fetch_offer(10, PRODUKT_URL)

    assert aufrufe == []
    assert repository.offers == []


def test_without_an_adapter_the_manual_path_remains(monkeypatch, registry_und_uhr, tmp_path):
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", tmp_path)
    monkeypatch.setattr(adapter, "_registry", None)
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst()

    with pytest.raises(ValidationError, match="record_offer"):
        service.fetch_offer(10, PRODUKT_URL)

    assert aufrufe == []
    assert repository.offers == []


def test_a_url_outside_the_url_pattern_is_refused(monkeypatch, registry_und_uhr):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst()

    with pytest.raises(ValidationError, match="kein url_pattern passt"):
        service.fetch_offer(10, "https://demoshop.example/suche?q=servo")

    assert aufrufe == []
    assert repository.offers == []


def test_a_robots_ban_ends_the_attempt(monkeypatch, registry_und_uhr):
    aufrufe = netz(monkeypatch, demoshop(robots=ROBOTS_VERBIETET))
    service, repository = dienst()

    with pytest.raises(ValidationError, match="robots.txt von demoshop.example verbietet"):
        service.fetch_offer(10, PRODUKT_URL)

    assert [request.url.path for request in aufrufe] == ["/robots.txt"]
    assert repository.offers == []


def test_a_currency_contradiction_writes_nothing(monkeypatch, registry_und_uhr):
    netz(monkeypatch, demoshop())
    service, repository = dienst(waehrung="EUR")

    with pytest.raises(ValidationError) as fehler:
        service.fetch_offer(10, PRODUKT_URL)

    assert "CHF" in str(fehler.value) and "EUR" in str(fehler.value)
    assert repository.offers == []


def test_a_missing_mandatory_field_writes_nothing(monkeypatch, registry_und_uhr):
    netz(monkeypatch, demoshop(seite=SEITE.replace('class="betrag"', 'class="weg"')))
    service, repository = dienst()

    with pytest.raises(ValidationError, match="«preis»"):
        service.fetch_offer(10, PRODUKT_URL)

    assert repository.offers == []


def test_a_temporary_failure_arrives_as_plain_text(monkeypatch, registry_und_uhr):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(503, text="wartung")

    netz(monkeypatch, handler)
    service, repository = dienst()

    with pytest.raises(ValidationError, match="HTTP 503"):
        service.fetch_offer(10, PRODUKT_URL)

    assert repository.offers == []


def test_a_redirect_inside_the_domain_stores_the_final_url(
    monkeypatch, registry_und_uhr
):
    ziel = "https://demoshop.example/produkt/mg996r-servo"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        if request.url.path == "/produkt/mg996r":
            return httpx.Response(301, headers={"Location": ziel})
        return httpx.Response(200, html=SEITE)

    netz(monkeypatch, handler)
    service, repository = dienst()

    ergebnis = service.fetch_offer(10, PRODUKT_URL)

    assert ergebnis["produkt_url"] == ziel
    assert ergebnis["quelle_url"] == ziel
    assert repository.offers[0]["produkt_url"] == ziel

