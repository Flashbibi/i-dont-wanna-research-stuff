from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import adapter, fetch
from app.jobs import parse_bom
from app.version import __version__
from app.web import create_app


class FakeRepository:
    def __init__(self):
        self.jobs = {}
        self.next_id = 1
        self.corrections = []

    def create_job(self, source_text, lines):
        job_id = self.next_id
        self.next_id += 1
        self.jobs[job_id] = {
            "id": job_id,
            "status": "offen",
            "quelltext": source_text,
            "lines": [
                {
                    "id": index + 100,
                    "position": line.position,
                    "originaltext": line.originaltext,
                    "suchtext": line.suchtext,
                    "menge": line.menge,
                    "status": "offen",
                    "kommentar": None,
                }
                for index, line in enumerate(lines)
            ],
        }
        return job_id

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def korrigiere_bestand(self, stock_id, delta, kommentar):
        self.corrections.append((stock_id, delta, kommentar))
        return {"id": stock_id, "menge": delta}

    def get_stock(self):
        return [
            {
                "id": 4,
                "bezeichnung": "Servo",
                "menge": 3,
                "einheit": "Stk",
                "artikelnummer": "MG90S",
                "shop_name": "Servo Shop",
                "produkt_url": "https://shop.example/servo",
                "aktualisiert_am": "2026-08-20T10:00:00+00:00",
            }
        ]

    def get_stock_bewegungen(self, limit=20):
        return [
            {
                "bezeichnung": "Servo",
                "delta": 3,
                "grund": "zugang_lieferung",
                "kommentar": None,
                "erstellt_am": "2026-08-20T10:00:00+00:00",
            }
        ]


def test_parse_bom_accepts_optional_quantity_prefix_and_skips_blank_lines():
    lines = parse_bom("2x MG996R Servo\n\n M3 Schrauben\n10 X Kabelbinder")

    assert [(line.position, line.menge, line.suchtext) for line in lines] == [
        (1, 2, "MG996R Servo"),
        (2, 1, "M3 Schrauben"),
        (3, 10, "Kabelbinder"),
    ]


def test_health_reports_migration_state_and_running_version():
    client = TestClient(create_app(FakeRepository(), lambda: 1))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "schema_version": 1,
        "app_version": __version__,
    }


def test_create_job_persists_lines_without_calculating_live():
    repository = FakeRepository()
    client = TestClient(create_app(repository, lambda: 1))

    response = client.post("/api/jobs", json={"parts": "2x Servo\nKabel"})

    assert response.status_code == 201
    assert response.json() == {"job_id": 1, "status": "offen", "line_count": 2}
    assert repository.jobs[1]["lines"][0]["menge"] == 2
    assert all(line["status"] == "offen" for line in repository.jobs[1]["lines"])


def test_web_job_creation_uses_shared_service_length_validation():
    client = TestClient(create_app(FakeRepository(), lambda: 1))

    response = client.post("/api/jobs", json={"parts": "x" * 501})

    assert response.status_code == 422
    assert "höchstens 500 Zeichen" in response.json()["detail"]


def test_create_job_rejects_empty_parts_list():
    client = TestClient(create_app(FakeRepository(), lambda: 1))

    response = client.post("/api/jobs", json={"parts": " \n "})

    assert response.status_code == 422
    assert "mindestens eine Position" in response.json()["detail"]


def test_get_job_returns_state_and_404_for_unknown_job():
    repository = FakeRepository()
    client = TestClient(create_app(repository, lambda: 1))
    client.post("/api/jobs", json={"parts": "Servo"})

    response = client.get("/api/jobs/1")
    missing = client.get("/api/jobs/999")

    assert response.status_code == 200
    assert response.json()["lines"][0]["suchtext"] == "Servo"
    assert missing.status_code == 404


def test_stock_correction_form_redirects_after_success():
    repository = FakeRepository()
    client = TestClient(create_app(repository, lambda: 1))

    response = client.post(
        "/bestand/korrektur",
        data={"stock_id": "4", "delta": "-1", "kommentar": "Inventur"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/bestand"
    assert repository.corrections == [(4, -1, "Inventur")]


def test_stock_page_renders_origin_amount_relative_time_and_movements():
    client = TestClient(create_app(FakeRepository(), lambda: 16))

    response = client.get("/bestand")

    assert response.status_code == 200
    for text in (
        "Servo",
        "MG90S",
        "Servo Shop",
        "3 Stk",
        "Letzte Bewegungen",
        "+3",
        "vor",
    ):
        assert text in response.text
    assert 'href="https://shop.example/servo"' in response.text


def test_stock_correction_validation_error_is_rendered_on_stock_page():
    class InvalidCorrectionRepository(FakeRepository):
        def korrigiere_bestand(self, stock_id, delta, kommentar):
            raise ValueError("Korrektur würde Bestand negativ machen")

    client = TestClient(create_app(InvalidCorrectionRepository(), lambda: 16))
    response = client.post(
        "/bestand/korrektur",
        data={"stock_id": "4", "delta": "-99", "kommentar": "Inventur"},
    )

    assert response.status_code == 422
    assert "Korrektur würde Bestand negativ machen" in response.text


# -- Angebote erfassen und auffrischen ---------------------------------------

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "adapters" / "demo"
SEITE = (FIXTURES / "produkt.html").read_text(encoding="utf-8")
PRODUKT_URL = "https://demoshop.example/produkt/mg996r"
FREMD_URL = "https://andershop.example/produkt/servo"
ROBOTS_ALLES = "User-agent: *\nAllow: /\n"
HEUTE = date(2026, 8, 25)


class OfferRepository(FakeRepository):
    """Ein Job mit einer Zeile, zwei Shops und einer tagesgenauen Angebotstabelle."""

    def __init__(self, *, shop_status: str = "bestaetigt"):
        super().__init__()
        self.jobs[7] = {
            "id": 7,
            "status": "in_arbeit",
            "quelltext": "1x Servo",
            "lines": [
                {
                    "id": 100,
                    "position": 1,
                    "originaltext": "1x Servo",
                    "suchtext": "Servo",
                    "menge": 1,
                    "status": "kandidaten",
                    "kommentar": None,
                }
            ],
        }
        self.jobs[8] = {
            "id": 8,
            "status": "offen",
            "quelltext": "1x Kabel",
            "lines": [],
        }
        self.shops = {
            1: {
                "id": 1,
                "name": "Demoshop",
                "url": "https://demoshop.example",
                "domain": "demoshop.example",
                "land": "CH",
                "status": shop_status,
                "versand_waehrung": "CHF",
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
        self.offers: dict[tuple, dict] = {}
        self.next_offer_id = 20

    def get_line(self, line_id):
        return {"id": line_id} if line_id == 100 else None

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

    def create_offer(self, **values):
        schluessel = (int(values["line_id"]), str(values["produkt_url"]), HEUTE)
        bestehend = self.offers.get(schluessel)
        if bestehend is None:
            self.next_offer_id += 1
        row = {
            "id": bestehend["id"] if bestehend else self.next_offer_id,
            "beobachtungstag": HEUTE,
            "position": 1,
            **values,
        }
        self.offers[schluessel] = row
        return dict(row)

    def get_offer(self, offer_id):
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
        # Das echte SQL joint bom_line dazu; ohne menge/suchtext rechnet der
        # Optimierer nicht.
        return {
            "offers": [
                {**row, "menge": 1, "suchtext": "Servo"} for row in self.offers.values()
            ],
            "shops": [dict(shop) for shop in self.shops.values()],
            "required_line_ids": [100],
            "lines": [{"id": 100, "position": 1, "suchtext": "Servo"}],
            "selected_assignments": None,
            "job_status": "in_arbeit",
        }


@pytest.fixture
def offer_client(monkeypatch):
    """Demo-Adapter, Fake-Netz, keine echte Wartezeit."""
    monkeypatch.setattr(adapter, "GEBUENDELT_DIR", FIXTURES)
    monkeypatch.setattr(adapter, "_registry", None)
    monkeypatch.delenv(adapter.ENV_ADAPTER_DIR, raising=False)
    monkeypatch.setattr(fetch, "_letzter_request", {})
    monkeypatch.setattr(fetch, "_robots_cache", {})
    monkeypatch.setattr(fetch, "_robots_sperren", {})
    monkeypatch.setattr(fetch, "_jetzt", lambda: 1_000.0)
    monkeypatch.setattr(fetch, "_schlafe", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLES)
        return httpx.Response(200, html=SEITE)

    monkeypatch.setattr(fetch, "_transport", lambda: httpx.MockTransport(handler))
    repository = OfferRepository()
    return TestClient(create_app(repository, lambda: 17)), repository


def test_an_offer_is_fetched_for_a_line_of_this_job(offer_client):
    client, repository = offer_client

    response = client.post(
        "/api/jobs/7/lines/100/offers/fetch", json={"produkt_url": PRODUKT_URL}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["produktname"] == "MG996R Servo Metallgetriebe"
    assert payload["erfasst_via"] == "adapter:demo"
    assert payload["extraktion"]["felder"]["lager_text"] == "an Lager (5 Stück)"
    assert len(repository.offers) == 1


def test_fetching_for_a_line_of_another_job_is_refused(offer_client):
    client, repository = offer_client

    response = client.post(
        "/api/jobs/8/lines/100/offers/fetch", json={"produkt_url": PRODUKT_URL}
    )

    assert response.status_code == 404
    assert "gehört nicht zu Job 8" in response.json()["detail"]
    assert repository.offers == {}


def test_a_page_without_an_adapter_answers_with_the_manual_way(offer_client):
    client, repository = offer_client

    response = client.post(
        "/api/jobs/7/lines/100/offers/fetch", json={"produkt_url": FREMD_URL}
    )

    assert response.status_code == 422
    assert "record_offer" in response.json()["detail"]
    assert repository.offers == {}


def test_a_manual_offer_is_recorded_as_unverified(offer_client):
    client, repository = offer_client

    response = client.post(
        "/api/jobs/7/lines/100/offers",
        json={
            "produkt_url": FREMD_URL,
            "produktname": "Servo XL",
            "preis": "7.50",
            "waehrung": "CHF",
            "lieferzeit_text": "2-3 Werktage",
            "lager_text": "an Lager",
            "artikelnummer": "XL-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    # Leer heisst «ungeprüft» - genau das weist die Oberfläche aus.
    assert payload["erfasst_via"] is None
    assert payload["lieferzeit_tage"] == 3
    assert payload["lieferzeit_text"] == "2-3 Werktage"
    assert payload["shop_id"] == 2


def test_a_manual_offer_for_an_unknown_shop_names_the_missing_step(offer_client):
    client, repository = offer_client

    response = client.post(
        "/api/jobs/7/lines/100/offers",
        json={
            "produkt_url": "https://nochnie.example/p/1",
            "produktname": "Servo",
            "preis": "7.50",
            "waehrung": "CHF",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "nochnie.example" in detail and "record_shop" in detail
    assert repository.offers == {}


def test_a_manual_offer_for_a_line_of_another_job_is_refused(offer_client):
    client, repository = offer_client

    response = client.post(
        "/api/jobs/8/lines/100/offers",
        json={
            "produkt_url": FREMD_URL,
            "produktname": "Servo",
            "preis": "7.50",
            "waehrung": "CHF",
        },
    )

    assert response.status_code == 404
    assert repository.offers == {}


def test_an_offer_is_refreshed_by_its_id(offer_client):
    client, repository = offer_client
    erst = client.post(
        "/api/jobs/7/lines/100/offers/fetch", json={"produkt_url": PRODUKT_URL}
    ).json()

    response = client.post(f"/api/offers/{erst['id']}/refresh")

    assert response.status_code == 200
    payload = response.json()
    assert payload["geaendert"] is False
    assert payload["vorher"]["preis_chf"] == "12.90"
    assert payload["nachher"]["id"] == erst["id"]
    assert payload["extraktion"]["adapter"] == "demo"


def test_refreshing_an_unknown_offer_is_a_plain_error(offer_client):
    client, _ = offer_client

    response = client.post("/api/offers/999/refresh")

    assert response.status_code == 422
    assert "Angebot 999 ist unbekannt" in response.json()["detail"]


def test_the_refresh_list_is_sorted_and_marks_adapter_coverage(offer_client):
    client, repository = offer_client
    client.post("/api/jobs/7/lines/100/offers/fetch", json={"produkt_url": PRODUKT_URL})
    client.post(
        "/api/jobs/7/lines/100/offers",
        json={
            "produkt_url": FREMD_URL,
            "produktname": "Servo XL",
            "preis": "7.50",
            "waehrung": "CHF",
        },
    )

    response = client.get("/api/jobs/7/refreshable")

    assert response.status_code == 200
    rows = response.json()
    # Deterministisch nach position, dann produkt_url.
    assert [row["produkt_url"] for row in rows] == [FREMD_URL, PRODUKT_URL]
    assert [row["adapter_verfuegbar"] for row in rows] == [False, True]
    assert rows[1]["preis_chf"] == "12.90"


def test_the_job_offer_payload_carries_the_capture_path(offer_client):
    client, repository = offer_client
    client.post("/api/jobs/7/lines/100/offers/fetch", json={"produkt_url": PRODUKT_URL})
    client.post(
        "/api/jobs/7/lines/100/offers",
        json={
            "produkt_url": FREMD_URL,
            "produktname": "Servo XL",
            "preis": "7.50",
            "waehrung": "CHF",
        },
    )

    matrix = client.post("/api/jobs/7/scenarios", json={"tempo": 0.5}).json()

    kandidaten = matrix["lines"][0]["candidates"]
    wege = {row["produkt_url"]: row["erfasst_via"] for row in kandidaten}
    assert wege == {PRODUKT_URL: "adapter:demo", FREMD_URL: None}
    # Und auch an der gewählten Matrixzelle, nicht nur in der Kandidatenliste.
    assert all("erfasst_via" in row for row in matrix["scenarios"][0]["lines"])
