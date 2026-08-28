"""Ohne das tagesgenaue Raster im Fake-Repository wäre die halbe Semantik von
``refresh_offer`` nicht prüfbar."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app import adapter, fetch
from app.procurement import ProcurementService, ValidationError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters" / "demo"
SEITE = (FIXTURES / "produkt.html").read_text(encoding="utf-8")

PRODUKT_URL = "https://demoshop.example/produkt/mg996r"
FREMD_URL = "https://andershop.example/produkt/servo"
ROBOTS_ALLES = "User-agent: *\nAllow: /\n"

HEUTE = date(2026, 8, 25)
GESTERN = HEUTE - timedelta(days=1)


class FakeRepository:
    """So viel Datenbank, wie der Auffrischweg anfasst - inklusive Tagesraster."""

    def __init__(self, *, status: str = "bestaetigt", waehrung: str = "CHF"):
        self.heute = HEUTE
        self.shops = {
            1: {
                "id": 1,
                "name": "Demoshop",
                "url": "https://demoshop.example",
                "domain": "demoshop.example",
                "land": "CH",
                "status": status,
                "versand_waehrung": waehrung,
            },
            2: {
                "id": 2,
                "name": "Andershop",
                "url": "https://andershop.example",
                "domain": "andershop.example",
                "land": "CH",
                "status": "bestaetigt",
                "versand_waehrung": "CHF",
            },
        }
        self.lines = {10: {"id": 10, "job_id": 5, "suchtext": "Servo", "menge": 1}}
        self.offers: dict[tuple[int, str, date], dict] = {}
        self.next_id = 20

    # -- Schreiben ----------------------------------------------------------

    def create_offer(self, **values):
        schluessel = (int(values["line_id"]), str(values["produkt_url"]), self.heute)
        bestehend = self.offers.get(schluessel)
        row = {
            "id": bestehend["id"] if bestehend else self._neue_id(),
            "beobachtungstag": self.heute,
            **values,
        }
        self.offers[schluessel] = row
        return dict(row)

    def _neue_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def seed(
        self,
        *,
        tag: date,
        preis: str,
        url: str = PRODUKT_URL,
        shop_id: int = 1,
        lieferzeit_tage: int | None = 3,
        lager_text: str | None = "an Lager (5 Stück)",
    ):
        """Eine Beobachtung von Hand setzen - so, wie sie an dem Tag entstand."""
        row = {
            "id": self._neue_id(),
            "line_id": 10,
            "shop_id": shop_id,
            "produkt_url": url,
            "produktname": "MG996R Servo Metallgetriebe",
            "preis_chf": Decimal(preis),
            "lieferzeit_tage": lieferzeit_tage,
            "lager_text": lager_text,
            "beobachtungstag": tag,
            "erfasst_via": "adapter:demo",
            "position": 1,
        }
        self.offers[(10, url, tag)] = row
        return row

    # -- Lesen --------------------------------------------------------------

    def get_line(self, line_id):
        return self.lines.get(line_id)

    def get_shop(self, shop_id):
        return self.shops.get(shop_id)

    def list_shops(self):
        return [dict(shop) for shop in self.shops.values()]

    def get_kurs(self, waehrung):
        return None

    def save_kurs(self, waehrung, kurs, geholt_am, quelle_url):
        return {
            "waehrung": waehrung,
            "kurs": kurs,
            "geholt_am": geholt_am,
            "quelle_url": quelle_url,
        }

    def get_offer(self, offer_id):
        """Dieselbe Regel wie das echte SQL: die jüngste Beobachtung der Reihe."""
        treffer = next(
            (row for row in self.offers.values() if row["id"] == offer_id), None
        )
        if treffer is None:
            return None
        reihe = [
            row
            for row in self.offers.values()
            if row["line_id"] == treffer["line_id"]
            and row["produkt_url"] == treffer["produkt_url"]
        ]
        return dict(max(reihe, key=lambda row: (row["beobachtungstag"], row["id"])))

    def optimization_input(self, job_id):
        return {
            "offers": [dict(row) for row in self.offers.values()],
            "shops": [dict(shop) for shop in self.shops.values()],
            "required_line_ids": [10],
            "lines": [{"id": 10, "position": 1, "suchtext": "Servo"}],
            "selected_assignments": None,
            "job_status": "in_arbeit",
        }


@pytest.fixture(autouse=True)
def registry_und_uhr(monkeypatch):
    """Demo-Adapter als einzige Registry, kein echtes Warten, fester Tag."""
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", FIXTURES)
    monkeypatch.setattr(adapter, "_registry", None)
    monkeypatch.delenv(adapter.ENV_ADAPTER_DIR, raising=False)
    monkeypatch.setattr(fetch, "_letzter_request", {})
    monkeypatch.setattr(fetch, "_robots_cache", {})
    monkeypatch.setattr(fetch, "_robots_sperren", {})
    monkeypatch.setattr(fetch, "_jetzt", lambda: 1_000.0)
    monkeypatch.setattr(fetch, "_schlafe", lambda _: None)
    monkeypatch.setattr(ProcurementService, "_heute", staticmethod(lambda: HEUTE))


def netz(monkeypatch, handler):
    aufrufe: list[httpx.Request] = []

    def merkend(request: httpx.Request) -> httpx.Response:
        aufrufe.append(request)
        return handler(request)

    monkeypatch.setattr(fetch, "_transport", lambda: httpx.MockTransport(merkend))
    return aufrufe


def demoshop(preis: str = "12.90", lager: str = "an Lager (5 Stück)"):
    seite = SEITE.replace("CHF&nbsp;12.90", f"CHF&nbsp;{preis}").replace(
        "an Lager (5 Stück)", lager
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(200, html=seite)

    return handler


def dienst(**kwargs) -> tuple[ProcurementService, FakeRepository]:
    repository = FakeRepository(**kwargs)
    return ProcurementService(repository), repository


def test_a_changed_price_shows_up_as_before_and_after(monkeypatch):
    netz(monkeypatch, demoshop(preis="11.50"))
    service, repository = dienst()
    alt = repository.seed(tag=GESTERN, preis="12.90")

    ergebnis = service.refresh_offer(alt["id"])

    assert ergebnis["vorher"]["preis_chf"] == Decimal("12.90")
    assert ergebnis["vorher"]["beobachtungstag"] == GESTERN
    assert ergebnis["nachher"]["preis_chf"] == Decimal("11.50")
    assert ergebnis["geaendert"] is True
    assert ergebnis["extraktion"]["felder"]["preis"] == "CHF 11.50"
    # Die heutige Beobachtung steht neben der von gestern, nicht an ihrer Stelle.
    assert repository.offers[(10, PRODUKT_URL, HEUTE)]["preis_chf"] == Decimal("11.50")
    assert repository.offers[(10, PRODUKT_URL, GESTERN)]["preis_chf"] == Decimal(
        "12.90"
    )


def test_an_unchanged_page_is_reported_as_unchanged(monkeypatch):
    netz(monkeypatch, demoshop())
    service, repository = dienst()
    alt = repository.seed(tag=GESTERN, preis="12.90")

    ergebnis = service.refresh_offer(alt["id"])

    assert ergebnis["geaendert"] is False
    assert ergebnis["nachher"]["lieferzeit_tage"] == 3
    assert ergebnis["nachher"]["erfasst_via"] == "adapter:demo"


def test_a_changed_stock_text_counts_as_a_change(monkeypatch):
    netz(monkeypatch, demoshop(lager="ausverkauft"))
    service, repository = dienst()
    alt = repository.seed(tag=GESTERN, preis="12.90")

    ergebnis = service.refresh_offer(alt["id"])

    assert ergebnis["geaendert"] is True
    assert ergebnis["nachher"]["lager_text"] == "ausverkauft"


def test_a_second_refresh_on_the_same_day_overwrites_todays_observation(monkeypatch):
    netz(monkeypatch, demoshop(preis="11.50"))
    service, repository = dienst()
    alt = repository.seed(tag=GESTERN, preis="12.90")

    erst = service.refresh_offer(alt["id"])
    netz(monkeypatch, demoshop(preis="10.00"))
    zweit = service.refresh_offer(alt["id"])

    # Kein zweiter Eintrag für heute - die Historie ist tagesgenau.
    heutige = [key for key in repository.offers if key[2] == HEUTE]
    assert len(heutige) == 1
    assert repository.offers[heutige[0]]["preis_chf"] == Decimal("10.00")
    # «vorher» ist der einzige Ort, an dem der überschriebene Wert noch steht.
    assert erst["nachher"]["preis_chf"] == Decimal("11.50")
    assert zweit["vorher"]["preis_chf"] == Decimal("11.50")
    assert zweit["vorher"]["beobachtungstag"] == HEUTE


def test_an_unknown_offer_is_a_plain_error(monkeypatch):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst()

    with pytest.raises(ValidationError, match="Angebot 999 ist unbekannt"):
        service.refresh_offer(999)

    assert aufrufe == []
    assert repository.offers == {}


def test_an_offer_without_an_adapter_leaves_the_database_alone(monkeypatch):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst()
    alt = repository.seed(tag=GESTERN, preis="7.00", url=FREMD_URL, shop_id=2)
    vorher = dict(repository.offers)

    with pytest.raises(ValidationError, match="record_offer"):
        service.refresh_offer(alt["id"])

    assert aufrufe == []
    assert repository.offers == vorher


def test_a_blocked_shop_leaves_the_database_alone(monkeypatch):
    aufrufe = netz(monkeypatch, demoshop())
    service, repository = dienst(status="gesperrt")
    alt = repository.seed(tag=GESTERN, preis="12.90")
    vorher = dict(repository.offers)

    with pytest.raises(ValidationError, match="gesperrt"):
        service.refresh_offer(alt["id"])

    assert aufrufe == []
    assert repository.offers == vorher


def test_a_failing_fetch_never_devalues_the_old_observation(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(503, text="wartung")

    netz(monkeypatch, handler)
    service, repository = dienst()
    alt = repository.seed(tag=GESTERN, preis="12.90")

    with pytest.raises(ValidationError, match="HTTP 503"):
        service.refresh_offer(alt["id"])

    assert list(repository.offers) == [(10, PRODUKT_URL, GESTERN)]
    assert repository.offers[(10, PRODUKT_URL, GESTERN)]["preis_chf"] == Decimal(
        "12.90"
    )


def test_the_refresh_list_says_which_offers_an_adapter_covers():
    service, repository = dienst()
    repository.seed(tag=HEUTE, preis="12.90")
    repository.seed(tag=HEUTE, preis="7.00", url=FREMD_URL, shop_id=2)

    liste = service.refreshable_offers(5)

    assert [row["produkt_url"] for row in liste] == [FREMD_URL, PRODUKT_URL]
    assert [row["adapter_verfuegbar"] for row in liste] == [False, True]
    assert liste[1]["preis_chf"] == "12.90"
    assert liste[1]["line_id"] == 10 and liste[1]["shop_id"] == 1
