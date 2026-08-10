from pathlib import Path

from fastapi.testclient import TestClient

from app.web import create_app


class UIRepository:
    def __init__(self):
        self.decisions = []
        self.decision_by_offer = {}
        self.shop_status = None
        self.arrived = []
        self.test_jobs = {}
        self.next_test_job_id = 9000

    def create_job(self, source_text, lines):
        return 7

    def get_job(self, job_id):
        if job_id != 7:
            return None
        return {
            "id": 7,
            "status": "in_arbeit",
            "quelltext": "2x Servo",
            "lines": [{"id": 10, "position": 1, "suchtext": "Servo", "menge": 2, "status": "kandidaten", "kommentar": None}],
        }

    def list_jobs(self, limit=20):
        return [{"id": 7, "status": "in_arbeit", "quelltext": "2x Servo", "erstellt_am": "heute", "line_count": 1}]

    def create_e2e_test_job(self):
        job_id = self.next_test_job_id
        self.next_test_job_id += 1
        record = {"job_id": job_id, "offer_id": 9001, "marker": "[E2E-TEST]"}
        self.test_jobs[job_id] = record
        return record

    def delete_e2e_test_job(self, job_id):
        if job_id not in self.test_jobs:
            raise ValueError("Nur markierte Test-Jobs dürfen gelöscht werden")
        del self.test_jobs[job_id]
        return {"job_id": job_id, "deleted": True}

    def get_job_detail(self, job_id):
        if job_id in self.test_jobs:
            return {
                "id": job_id,
                "status": "test",
                "quelltext": "[E2E-TEST] Klickhygiene",
                "lines": [{
                    "id": 9002,
                    "position": 1,
                    "suchtext": "[E2E-TEST] Wegwerfangebot",
                    "menge": 1,
                    "status": "kandidaten",
                    "kommentar": None,
                    "offers": [{
                        "id": 9001,
                        "produktname": "[E2E-TEST] Wegwerfprodukt",
                        "produkt_url": "https://example.invalid/e2e",
                        "preis_chf": "1.00",
                        "lieferzeit_tage": 1,
                        "lieferzeit_text": "1 Testtag",
                        "lager_text": "Testbestand",
                        "shop_id": 1,
                        "shop_name": "E2E Test",
                        "shop_status": "bestaetigt",
                        "lieferzeit_default_tage": 1,
                        "decision": self.decision_by_offer.get(9001),
                    }],
                }],
            }
        job = self.get_job(job_id)
        if not job:
            return None
        job["lines"][0]["offers"] = [
            {
                "id": 31,
                "produktname": "MG996R Servo",
                "produkt_url": "https://shop.example.ch/mg996r",
                "preis_chf": "10.00",
                "lieferzeit_tage": 1,
                "lieferzeit_text": "1 Tag ab Lager",
                "lager_text": "Filiale grün; 5 Stück lagernd",
                "shop_id": 1,
                "shop_name": "Servo Shop",
                "shop_status": "ungeprueft",
                "lieferzeit_default_tage": 3,
                "decision": self.decision_by_offer.get(31),
            },
            {
                "id": 32,
                "produktname": "Servo ohne Lieferangabe",
                "produkt_url": "https://shop.example.ch/servo-ohne-angabe",
                "preis_chf": "12.00",
                "lieferzeit_tage": None,
                "lieferzeit_text": None,
                "lager_text": None,
                "shop_id": 1,
                "shop_name": "Servo Shop",
                "shop_status": "ungeprueft",
                "lieferzeit_default_tage": 3,
                "decision": None,
            },
        ]
        return job

    def record_decision(self, offer_id, status):
        self.decisions.append((offer_id, status))
        self.decision_by_offer[offer_id] = status
        return {"offer_id": offer_id, "status": status}

    def optimization_input(self, job_id):
        return {
            "offers": [{
                "id": 31,
                "line_id": 10,
                "shop_id": 1,
                "preis_chf": "10",
                "menge": 2,
                "lieferzeit_tage": 2,
                "lieferzeit_text": "2 Tage",
                "produktname": "MG996R Servo",
                "produkt_url": "https://shop.example.ch/mg996r",
                "suchtext": "Servo",
                "position": 1,
                "override_status": self.decision_by_offer.get(31),
            }],
            "shops": [{"id": 1, "name": "Servo Shop", "url": "https://shop.example.ch", "versand_chf": "8", "gratis_ab_chf": None, "mindestbestellwert_chf": None, "lieferzeit_default_tage": 3}],
            "required_line_ids": [10],
            "lines": [{"id": 10, "position": 1, "suchtext": "Servo"}],
        }

    def list_purchases(self):
        return [{"id": 90, "job_id": 7, "total_chf": "28.00", "bestellt_am": "heute", "angekommen_am": None, "items": [{"produktname": "MG996R Servo", "menge": 2}]}]

    def repeat_purchase(self, purchase_id):
        return 8

    def mark_purchase_arrived(self, purchase_id):
        self.arrived.append(purchase_id)
        return {"id": purchase_id, "angekommen_am": "jetzt"}

    def list_shops(self):
        return [{"id": 1, "name": "Servo Shop", "url": "https://shop.example.ch", "status": "ungeprueft", "versand_chf": "8.00", "lieferzeit_default_tage": 3}]

    def update_shop_status(self, shop_id, status):
        self.shop_status = (shop_id, status)
        return {"id": shop_id, "status": status}


def client_and_repo():
    repository = UIRepository()
    return TestClient(create_app(repository, lambda: 1)), repository


def test_home_has_job_form_and_recent_jobs():
    client, _ = client_and_repo()

    response = client.get("/")

    assert response.status_code == 200
    assert 'name="parts"' in response.text
    assert "2x MG996R Servo" in response.text
    assert "Job #7" in response.text


def test_form_creates_saved_job_and_redirects_to_job_page():
    client, _ = client_and_repo()

    response = client.post("/jobs", data={"parts": "2x Servo"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/7"


def test_job_page_shows_progressive_candidates_badge_and_decision_buttons():
    client, repository = client_and_repo()

    page = client.get("/jobs/7")
    decision = client.post("/api/offers/31/decision", json={"status": "pin"})

    assert page.status_code == 200
    assert "MG996R Servo" in page.text
    assert "ungeprueft" in page.text
    assert "Pin" in page.text
    assert decision.status_code == 200
    assert repository.decisions == [(31, "pin")]


def test_dashboard_uses_sidebar_and_places_scenarios_before_collapsible_offers():
    client, _ = client_and_repo()

    page = client.get("/jobs/7").text

    assert '<aside class="sidebar">' in page
    assert all(label in page for label in ("Jobs", "Historie", "Shops", "Bestand"))
    assert page.index('id="scenarios"') < page.index('class="bom"')
    assert '<details class="offers-drawer" open>' in page
    assert 'id="tempo"' in page


def test_e2e_jobs_are_marker_guarded_disposable_and_not_real_job_ids():
    client, repository = client_and_repo()
    marker = {"X-E2E-Marker": "beschaffung-e2e-disposable"}

    assert client.post("/api/e2e/jobs").status_code == 404
    created = client.post("/api/e2e/jobs", headers=marker)
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    assert job_id >= 9000
    assert job_id in repository.test_jobs
    assert "[E2E-TEST]" in client.get(f"/jobs/{job_id}").text

    deleted = client.delete(f"/api/e2e/jobs/{job_id}", headers=marker)
    assert deleted.status_code == 200
    assert job_id not in repository.test_jobs
    assert client.delete("/api/e2e/jobs/7", headers=marker).status_code == 422


def test_browser_e2e_creates_and_cleans_marked_job_instead_of_using_job_one():
    script = Path("tests/e2e/decision_click.mjs").read_text()

    assert "OFFER_ID" not in script
    assert "/jobs/1" not in script
    assert "/api/e2e/jobs" in script
    assert "method: 'DELETE'" in script


def test_unknown_delivery_is_rendered_explicitly_in_assignments():
    script = Path("static/variants.js").read_text()

    assert "value == null" in script
    assert "Lieferzeit unbekannt" in script
    assert "kein belegter Shop-Standard" in script

def test_decision_has_form_fallback_and_persists_after_redirect_reload():
    client, repository = client_and_repo()

    initial = client.get("/jobs/7")
    submitted = client.post(
        "/offers/31/decision",
        data={"status": "pin", "job_id": "7"},
        follow_redirects=False,
    )
    reloaded = client.get("/jobs/7")

    assert 'action="/offers/31/decision"' in initial.text
    assert 'role="status"' in initial.text
    assert submitted.status_code == 303
    assert submitted.headers["location"] == "/jobs/7#offer-31"
    assert repository.decisions == [(31, "pin")]
    assert 'id="offer-31"' in reloaded.text
    assert "decision-bestaetigt" in reloaded.text
    assert "Gepinnt" in reloaded.text


def test_job_page_shows_literal_delivery_and_lager_text_with_labeled_fallback():
    client, _ = client_and_repo()

    page = client.get("/jobs/7")

    assert "1 Tag" in page.text
    assert "1 Tage" not in page.text
    assert "1 Tag ab Lager" in page.text
    assert "Filiale grün; 5 Stück lagernd" in page.text
    assert "3 Tage" in page.text
    assert "Schätzung (Shop-Standard)" in page.text
    assert "Lagerstatus nicht angegeben" in page.text


def test_variants_page_and_server_side_tempo_api_include_links_and_totals():
    client, _ = client_and_repo()

    page = client.get("/jobs/7/variants")
    variants = client.get("/api/jobs/7/variants?tempo=0.5")
    scenarios = client.post(
        "/api/jobs/7/scenarios",
        json={"pins": {}, "excludes": [], "tempo": 0.5},
    )

    assert page.status_code == 200
    assert 'type="range"' in page.text
    payload = variants.json()
    assert payload[0]["total_chf"] == "28.00"
    assert payload[0]["lines"][0]["produkt_url"].endswith("/mg996r")
    assert payload[0]["shops"][0]["name"] == "Servo Shop"
    assert scenarios.status_code == 200
    grouped = scenarios.json()["scenarios"]
    assert len(grouped) == 1
    assert grouped[0]["keys"] == ["cheapest", "fastest", "one_shop", "balanced"]
    assert grouped[0]["same_result_note"]


def test_history_repeat_arrival_and_shop_moderation_are_available():
    client, repository = client_and_repo()

    history = client.get("/history")
    repeated = client.post("/purchases/90/repeat", follow_redirects=False)
    arrived = client.post("/purchases/90/arrived", follow_redirects=False)
    shops = client.get("/shops")
    updated = client.post("/shops/1/status", data={"status": "bestaetigt"}, follow_redirects=False)

    assert "Nochmal bestellen" in history.text
    assert "Angekommen" in history.text
    assert repeated.headers["location"] == "/jobs/8"
    assert arrived.status_code == 303
    assert repository.arrived == [90]
    assert "Servo Shop" in shops.text
    assert updated.status_code == 303
    assert repository.shop_status == (1, "bestaetigt")
