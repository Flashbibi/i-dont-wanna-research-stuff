# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.version import __version__
from app.web import create_app


class UIRepository:
    def __init__(self):
        self.decisions = []
        self.decision_by_offer = {}
        self.shop_status = None
        self.arrived = []
        self.test_jobs = {}
        self.next_test_job_id = 9000
        self.selected_assignments = None
        self.purchases_created = []
        self.platform_writes = []
        self.product_id_writes = []
        self.artikelnummer_writes = []
        self.lieferziel_writes = []
        self.shop_profile_writes = []
        self.deleted_jobs = []

    def create_job(self, source_text, lines):
        return 7

    def get_job(self, job_id):
        if job_id == 13 and job_id not in self.deleted_jobs:
            return {
                "id": 13,
                "status": "offen",
                "quelltext": "Versehentlich angelegt",
                "lines": [{
                    "id": 46,
                    "position": 1,
                    "suchtext": "Unberührte Position",
                    "menge": 1,
                    "status": "offen",
                    "kommentar": None,
                }],
            }
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

    def delete_unstarted_job(self, job_id):
        if job_id != 13 or job_id in self.deleted_jobs:
            raise ValueError("Job ist nicht mehr unberührt und darf nicht gelöscht werden")
        self.deleted_jobs.append(job_id)
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
        if job_id == 13:
            job["lines"][0]["offers"] = []
            return job
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
            "lines": [{"id": 10, "position": 1, "suchtext": "Servo", "menge": 2, "status": "kandidaten", "kommentar": None}],
            "selected_assignments": self.selected_assignments,
        }

    def save_job_selection(self, job_id, assignments):
        self.selected_assignments = assignments
        return {"job_id": job_id, "selected_assignments": assignments}

    def create_purchase(self, job_id, variant, ordered_at, promised_days):
        purchase = {"id": 91, "job_id": job_id, "variante": variant}
        self.purchases_created.append((job_id, variant, promised_days))
        return purchase

    def list_purchases(self):
        return [{"id": 90, "job_id": 7, "total_chf": "28.00", "bestellt_am": "heute", "angekommen_am": None, "items": [{"produktname": "MG996R Servo", "menge": 2}]}]

    def repeat_purchase(self, purchase_id):
        return 8

    def mark_purchase_arrived(self, purchase_id):
        self.arrived.append(purchase_id)
        return {"id": purchase_id, "angekommen_am": "jetzt"}

    def is_test_job(self, job_id):
        return job_id in self.test_jobs

    def get_shop(self, shop_id):
        if shop_id != 1:
            return None
        return {
            "id": 1,
            "name": "Servo Shop",
            "url": "https://shop.example.ch",
            "domain": "shop.example.ch",
            "status": "ungeprueft",
            "plattform": None,
            "plattform_beleg": None,
            "plattform_geprueft_am": None,
        }

    def save_shop_platform(self, shop_id, plattform, plattform_beleg):
        self.platform_writes.append((shop_id, plattform, plattform_beleg))
        return {"id": shop_id, "plattform": plattform, "plattform_beleg": plattform_beleg}

    def save_offer_product_ids(self, produkt_ids):
        self.product_id_writes.append(dict(produkt_ids))
        return len(produkt_ids)

    def save_offer_artikelnummern(self, artikelnummern):
        self.artikelnummer_writes.append(dict(artikelnummern))
        return len(artikelnummern)

    def list_lieferziele(self):
        return [
            {"id": 1, "name": "Zuhause (CH)", "adresse": "Heimadresse", "land": "CH",
             "waehrung": "CHF", "aufschlag_chf": "0.00", "zuschlag_tage": 0, "shop_count": 1},
            {"id": 2, "name": "Postfach (DE)", "adresse": "Grenzstrasse 1", "land": "DE",
             "waehrung": "EUR", "aufschlag_chf": "25.00", "zuschlag_tage": 3, "shop_count": 0},
        ]

    def get_lieferziel(self, lieferziel_id):
        return next((z for z in self.list_lieferziele() if z["id"] == lieferziel_id), None)

    def lieferziele_fuer_land(self, land):
        return [z for z in self.list_lieferziele() if z["land"] == land]

    def create_lieferziel(self, **values):
        self.lieferziel_writes.append(dict(values))
        return {"id": 3, **values}

    def update_lieferziel(self, lieferziel_id, **values):
        self.lieferziel_writes.append({"id": lieferziel_id, **values})
        return {"id": lieferziel_id, **values}

    def list_shops(self):
        return [{"id": 1, "name": "Servo Shop", "url": "https://shop.example.ch", "status": "ungeprueft", "versand_chf": "8.00", "lieferzeit_default_tage": 3}]

    def update_shop_status(self, shop_id, status):
        self.shop_status = (shop_id, status)
        return {"id": shop_id, "status": status}

    def update_shop_profile(self, shop_id, **values):
        self.shop_profile_writes.append({"id": shop_id, **values})
        return {"id": shop_id, **values}


def client_and_repo():
    repository = UIRepository()
    return TestClient(create_app(repository, lambda: 1)), repository


def csrf_token_from(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


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


def test_job_page_loads_candidates_from_matrix_api_and_decision_buttons_are_wired():
    client, repository = client_and_repo()

    page = client.get("/jobs/7")
    matrix = client.post("/api/jobs/7/scenarios", json={"tempo": 0.5}).json()
    decision = client.post("/api/offers/31/decision", json={"status": "pin"})
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")

    assert page.status_code == 200
    assert matrix["lines"][0]["candidates"][0]["produktname"] == "MG996R Servo"
    assert "Pinnen" in script and "Pin lösen" in script and "Ausschliessen" in script
    assert "candidate.provenienz_text" in script
    assert decision.status_code == 200
    assert repository.decisions == [(31, "pin")]


def test_job_page_uses_prototype_c_matrix_and_removes_old_card_sections():
    client, _ = client_and_repo()

    page = client.get("/jobs/7").text

    assert '<aside class="sidebar">' in page
    assert all(label in page for label in ("Jobs", "Historie", "Shops", "Bestand"))
    assert 'id="matrix-root"' in page
    assert 'id="railsum"' in page
    assert 'id="railorder"' in page
    assert 'id="tempo"' in page
    assert "Bestellpläne im Vergleich" in page
    assert ">in Arbeit<" in page
    assert 'id="scenarios"' not in page
    assert "Bestellszenarien" not in page
    assert "Angebote und Overrides" not in page
    assert "Szenarien separat öffnen" not in page


def test_untouched_job_page_shows_guarded_delete_button_only_for_that_job():
    client, _ = client_and_repo()

    untouched = client.get("/jobs/13").text
    touched = client.get("/jobs/7").text

    assert 'action="/jobs/13/delete"' in untouched
    assert 'name="confirm_job_id" value="13"' in untouched
    assert 'name="csrf_token"' in untouched
    assert "Job #13 wirklich löschen?" in untouched
    assert "Job löschen" in untouched
    assert "/jobs/7/delete" not in touched


def test_footer_names_the_running_version_before_the_source_link():
    client, _ = client_and_repo()

    footer = re.search(r"<footer>(.*?)</footer>", client.get("/").text, re.S)

    assert footer is not None
    text = " ".join(re.sub(r"<[^>]+>", " ", footer.group(1)).split())
    assert text == f"v{__version__} · Source (AGPL-3.0)"
    assert text == "v0.1.0 · Source (AGPL-3.0)"


def test_stylesheet_changes_use_a_fresh_cache_version():
    base = Path("templates/base.html").read_text(encoding="utf-8")

    assert '/static/app.css?v=12' in base


def test_delete_job_form_uses_guarded_service_and_redirects_home():
    client, repository = client_and_repo()
    csrf_token = csrf_token_from(client.get("/jobs/13").text)

    deleted = client.post(
        "/jobs/13/delete",
        data={"confirm_job_id": "13", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/"
    assert repository.deleted_jobs == [13]
    assert client.get("/jobs/13").status_code == 404


def test_delete_job_form_rejects_mismatched_confirmation_without_deleting():
    client, repository = client_and_repo()
    csrf_token = csrf_token_from(client.get("/jobs/13").text)

    rejected = client.post(
        "/jobs/13/delete",
        data={"confirm_job_id": "12", "csrf_token": csrf_token},
        follow_redirects=False,
    )

    assert rejected.status_code == 422
    assert repository.deleted_jobs == []


def test_delete_job_form_rejects_cross_site_submission_without_csrf_token():
    client, repository = client_and_repo()

    forged = client.post(
        "/jobs/13/delete",
        data={"confirm_job_id": "13"},
        follow_redirects=False,
    )

    assert forged.status_code == 403
    assert repository.deleted_jobs == []


def test_delete_job_form_rejects_an_attacker_injected_cookie_value():
    client, repository = client_and_repo()
    known_value = "a" * 43
    client.cookies.set(
        "beschaffung_csrf", known_value, domain="testserver.local", path="/"
    )

    forged = client.post(
        "/jobs/13/delete",
        data={"confirm_job_id": "13", "csrf_token": known_value},
        follow_redirects=False,
    )

    assert forged.status_code == 403
    assert repository.deleted_jobs == []


@pytest.mark.parametrize("malformed_token", ["é", "a" * 63, "g" * 64])
def test_delete_job_form_rejects_malformed_form_tokens(malformed_token):
    client, repository = client_and_repo()
    client.get("/jobs/13")

    rejected = client.post(
        "/jobs/13/delete",
        data={"confirm_job_id": "13", "csrf_token": malformed_token},
        follow_redirects=False,
    )

    assert rejected.status_code == 403
    assert repository.deleted_jobs == []


def test_job_page_replaces_malformed_csrf_cookie_with_a_fresh_token():
    client, _ = client_and_repo()
    client.cookies.set(
        "beschaffung_csrf", '""', domain="testserver.local", path="/"
    )

    page = client.get("/jobs/13")
    token = client.cookies.get(
        "beschaffung_csrf", domain="testserver.local", path="/"
    )

    assert page.status_code == 200
    assert token and len(token) >= 32


def test_job_matrix_browser_uses_only_server_planning_and_reference_wording():
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")

    assert "/scenarios" in script
    assert "/selection" in script
    assert "/delta" in script
    assert "/decision" in script
    assert "/purchase" in script
    assert "function optimize" not in script
    assert "CHFDAY" not in script
    assert "Ändert bei diesem Angebots-Pool nichts:" in script
    assert "Diesen Plan wählen" in script
    assert "✓ Gewählt" in script
    assert "Lieferzeit unbekannt" in script
    assert "shipping_chf" not in script
    assert "free_shipping_from_chf" not in script
    assert "item_count" not in script


def test_job_matrix_offers_an_explicit_all_candidates_view():
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")

    assert "showAllCandidates" in script
    assert "data-show-all-candidates" in script
    assert "Alle ${offerCount} Angebote anzeigen" in script


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
    script = Path("tests/e2e/decision_click.mjs").read_text(encoding="utf-8")
    database = Path("app/database.py").read_text(encoding="utf-8")

    assert "OFFER_ID" not in script
    assert "/jobs/1" not in script
    assert "/api/e2e/jobs" in script
    assert "method: 'DELETE'" in script
    assert "selectionPersisted" in script
    assert "incompletePurchaseBlocked" in script
    assert "tempoVerdictVisible" in script
    assert "purchasePersisted" in script
    assert "ORDER BY id LIMIT 3" in database
    assert "DELETE FROM purchase_item" in database
    assert "DELETE FROM purchase WHERE job_id" in database


def test_cart_e2e_uses_a_disposable_job_and_never_touches_a_real_shop():
    script = Path("tests/e2e/cart_fill_click.mjs").read_text(encoding="utf-8")

    assert "/api/e2e/jobs" in script
    assert "method: 'DELETE'" in script
    # Der Stub wird im Test angehängt, nicht in der Oberfläche verdrahtet.
    assert "page.route('**/shops/*/cart'" in script
    assert "searchParams.set('stub', stubMode)" in script
    assert "'X-E2E-Marker'" in script
    # Zustandswechsel, Erfolg, Fehlerfall und Reload-Konsistenz.
    assert "fillButtonVisible" in script
    assert "verifiedCartVisible" in script
    assert "cookieHandoverVisible" in script
    assert "consistentAfterReload" in script
    assert "mismatchDiffVisible" in script
    assert "cartResponses, [200, 409, 200]" in script
    # Der unvermeidbare 409-Konsoleneintrag wird präzisiert, nicht toleriert:
    # genau einer, auf die Cart-URL gescoped, nur im Mismatch-Leg.
    assert "expectedConflictLogs" in script
    assert "assert.deepEqual(evidence.consoleErrors, [])" in script
    assert "expectedConflictLogs.length, 1" in script


def test_extension_e2e_loads_the_real_extension_and_documents_its_fallback():
    script = Path("tests/e2e/extension_handoff_click.mjs").read_text(encoding="utf-8")

    assert "/api/e2e/jobs" in script
    assert "method: 'DELETE'" in script
    assert "--load-extension=" in script
    # Ohne Erweiterung nur Kopierflow, mit Erweiterung der Knopf.
    assert "withoutExtensionCopyOnly" in script
    assert "Im Browser übernehmen" in script
    # Cookie auf der Shop-Origin und geöffneter Tab sind der Beweis.
    assert "context.cookies(COOKIE_ORIGIN)" in script
    assert "waitForEvent('page')" in script
    # Welche Variante lief, steht in der Ausgabe.
    assert "geladene-erweiterung" in script and "gestubtes-ready-signal" in script
    # Tab-Ziel lokal, Cookie-Ziel bleibt https.
    assert "payload.uebergabe_url = `${baseUrl}/shops`" in script
    assert "copyFlowStillPresent" in script


def test_unknown_delivery_is_rendered_explicitly_in_assignments():
    script = Path("static/variants.js").read_text(encoding="utf-8")

    assert "value == null" in script
    assert "Lieferzeit unbekannt" in script
    assert "kein belegter Shop-Standard" in script

def test_decision_has_form_fallback_and_persists_in_matrix_api():
    client, repository = client_and_repo()
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")

    submitted = client.post(
        "/offers/31/decision",
        data={"status": "pin", "job_id": "7"},
        follow_redirects=False,
    )
    reloaded = client.post("/api/jobs/7/scenarios", json={"tempo": 0.5}).json()

    assert 'method="post" action="/offers/${candidate.offer_id}/decision"' in script
    assert 'name="status" value="pin"' in script
    assert submitted.status_code == 303
    assert submitted.headers["location"] == "/jobs/7#offer-31"
    assert repository.decisions == [(31, "pin")]
    assert reloaded["pins"] == {"10": 31}


def test_matrix_api_and_browser_preserve_delivery_provenance_and_fallback_wording():
    client, _ = client_and_repo()

    matrix = client.post("/api/jobs/7/scenarios", json={"tempo": 0.5}).json()
    candidate = matrix["lines"][0]["candidates"][0]
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")

    assert candidate["lieferzeit_chip"] == "2 Tage"
    assert candidate["lieferzeit_text"] == "2 Tage"
    assert "Lieferzeit nicht angegeben" in script
    assert "Lagerstatus nicht angegeben" in script
    assert "Lieferzeit unbekannt" in script


def test_matrix_selection_and_delta_endpoints_use_server_side_planning():
    client, repository = client_and_repo()
    matrix = client.post("/api/jobs/7/scenarios", json={"tempo": 0.5}).json()
    assignments = matrix["scenarios"][0]["assignments"]

    selected = client.put(
        "/api/jobs/7/selection", json={"assignments": assignments}
    )
    delta = client.post(
        "/api/jobs/7/lines/10/delta",
        json={
            "offer_id": 31,
            "base_assignments": assignments,
            "tempo": 0.5,
        },
    )

    assert selected.status_code == 200
    assert repository.selected_assignments == {"10": 31}
    assert delta.status_code == 200
    assert delta.json()["delta_chf"] == "0.00"


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
    assert grouped[0]["keys"] == ["cheapest", "fastest", "one_shop", "balanced", "only_ch"]
    assert grouped[0]["same_result_note"]


def test_cart_shops_endpoint_marks_which_shops_can_be_filled():
    client, _ = client_and_repo()

    rows = client.get("/api/jobs/7/cart-shops").json()

    assert rows == [
        {
            "shop_id": 1,
            "shop_name": "Servo Shop",
            "shop_url": "https://shop.example.ch",
            "plattform": None,
            "plattform_geprueft": False,
            "kann_fuellen": True,
        }
    ]


def test_cart_fill_rejects_an_unknown_shop_without_touching_the_network():
    client, _ = client_and_repo()

    response = client.post("/api/jobs/7/shops/99/cart")

    assert response.status_code == 422
    assert "99" in response.json()["detail"]


def test_cart_fill_maps_each_outcome_to_its_own_status_code():
    """Wiederholbar, belegter Unterschied und Rest müssen unterscheidbar sein."""
    from app.cart import CartError, CartTemporaryError, CartVerificationError

    client, _ = client_and_repo()
    procurement = client.app.state.procurement

    cases = [
        (CartTemporaryError("Shop war nicht erreichbar"), 503),
        (CartVerificationError(["Zwischensumme weicht ab: erfasst CHF 3.90, Korb CHF 4.20."]), 409),
        (CartError("Produktseite nennt keine product_id"), 422),
    ]
    for error, expected in cases:
        def explode(*_args, _error=error, **_kwargs):
            raise _error

        procurement.fill_cart = explode
        response = client.post("/api/jobs/7/shops/1/cart")

        assert response.status_code == expected
        assert response.json()["detail"] == str(error)


MARKER = {"X-E2E-Marker": "beschaffung-e2e-disposable"}


def client_with_test_job():
    client, repository = client_and_repo()
    job_id = client.post("/api/e2e/jobs", headers=MARKER).json()["job_id"]
    return client, repository, job_id


def test_cart_stub_is_unreachable_without_the_e2e_marker():
    client, repository, job_id = client_with_test_job()

    assert client.post(f"/api/jobs/{job_id}/shops/1/cart?stub=ok").status_code == 404
    assert repository.platform_writes == []
    assert repository.product_id_writes == []


def test_cart_stub_never_applies_to_a_real_job_even_with_the_marker():
    """Ein gestubtes «Korb geprüft ✓» darf auf einem echten Job nicht existieren."""
    client, repository, test_job_id = client_with_test_job()

    real = client.post("/api/jobs/7/shops/1/cart?stub=ok", headers=MARKER)
    mismatch = client.post("/api/jobs/7/shops/1/cart?stub=mismatch", headers=MARKER)

    assert real.status_code == 404
    assert mismatch.status_code == 404
    # Der Marker wirkt weiterhin - nur eben ausschliesslich auf Testjobs.
    assert client.post(
        f"/api/jobs/{test_job_id}/shops/1/cart?stub=ok", headers=MARKER
    ).status_code == 200
    assert repository.platform_writes == []
    assert repository.product_id_writes == []


def test_cart_stub_rejects_an_unknown_mode():
    client, _, job_id = client_with_test_job()

    response = client.post(f"/api/jobs/{job_id}/shops/1/cart?stub=erfunden", headers=MARKER)

    assert response.status_code == 422


def test_cart_stub_verifies_a_matching_cart_without_writing_anything():
    client, repository, job_id = client_with_test_job()

    response = client.post(f"/api/jobs/{job_id}/shops/1/cart?stub=ok", headers=MARKER)

    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "uebergabe"
    assert payload["verifiziert"] is True
    assert payload["artikel_anzahl"] == 2
    assert payload["total_chf"] == "20.00"
    assert payload["cookie"]["name"] == "OCSESSID"
    # Der Testlauf darf weder eine Plattform festschreiben noch Produkt-IDs
    # echter Angebote überschreiben.
    assert repository.platform_writes == []
    assert repository.product_id_writes == []


def test_cart_stub_mismatch_produces_the_blocking_diff():
    client, _, job_id = client_with_test_job()

    response = client.post(f"/api/jobs/{job_id}/shops/1/cart?stub=mismatch", headers=MARKER)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "erfasst CHF 20.00" in detail
    assert "Korb CHF 20.60" in detail


def test_cart_fill_reports_a_platform_specific_handover_url():
    client, _, job_id = client_with_test_job()

    payload = client.post(f"/api/jobs/{job_id}/shops/1/cart?stub=ok", headers=MARKER).json()

    # Additiv: cart_url bleibt unverändert, uebergabe_url kommt dazu.
    assert payload["cart_url"].endswith("index.php?route=checkout/cart")
    assert payload["uebergabe_url"] == payload["cart_url"]


def test_extension_endpoint_reports_the_deployed_version_and_files():
    client, _ = client_and_repo()

    info = client.get("/api/extension").json()

    manifest = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
    assert info["version"] == manifest["version"]
    assert info["download_url"] == "/extension.zip"
    assert set(info["dateien"]) == {"manifest.json", "background.js", "content.js"}


def test_extension_zip_is_the_deployed_directory_and_is_deterministic():
    client, _ = client_and_repo()

    first = client.get("/extension.zip")
    second = client.get("/extension.zip")

    assert first.status_code == 200
    assert first.headers["content-type"] == "application/zip"
    # Byte-identisch: der Download ist per Konstruktion der deployte Stand.
    assert first.content == second.content

    archive = zipfile.ZipFile(io.BytesIO(first.content))
    on_disk = {
        path.relative_to(Path("extension")).as_posix()
        for path in Path("extension").rglob("*")
        if path.is_file()
    }
    assert set(archive.namelist()) == on_disk
    assert archive.testzip() is None

    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["version"] == client.get("/api/extension").json()["version"]
    assert f'beschaffung-extension-{manifest["version"]}.zip' in first.headers["content-disposition"]


def test_extension_manifest_carries_both_background_keys_and_a_narrow_radius():
    manifest = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    # Ein Codebase für Chromium und Firefox.
    assert manifest["background"]["service_worker"] == "background.js"
    assert manifest["background"]["scripts"] == ["background.js"]
    assert manifest["browser_specific_settings"]["gecko"]["id"]
    # Content-Script ausschliesslich auf der Tool-Origin.
    assert manifest["content_scripts"][0]["matches"] == ["http://192.168.1.60:8000/*"]
    # Enger Radius: kein <all_urls>, und externally_connectable gibt es nicht.
    assert manifest["permissions"] == ["cookies"]
    assert "<all_urls>" not in manifest["host_permissions"]
    assert "*://*.bastelgarage.ch/*" in manifest["host_permissions"]
    assert "externally_connectable" not in manifest


def test_extension_scripts_use_the_cross_browser_api_and_never_log_cookies():
    background = Path("extension/background.js").read_text(encoding="utf-8")
    content = Path("extension/content.js").read_text(encoding="utf-8")

    for source in (background, content):
        assert "globalThis.browser ?? globalThis.chrome" in source
        # Cookie-Werte tauchen in keiner Ausgabe auf.
        assert "console.log" not in source
    assert "cookies.set" in background
    assert "tabs.create" in background
    # Herkunft wird strikt geprüft, sonst könnte jede eingebettete Seite senden.
    assert "event.source === window" in content
    assert "event.origin === window.location.origin" in content
    assert "beschaffung/extension-ready" in content


def test_cart_browser_shows_states_handover_and_keeps_expected_outcomes_neutral():
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")
    css = Path("static/app.css").read_text(encoding="utf-8")

    assert "/cart-shops" in script
    assert "/cart`" in script
    assert "Warenkorb füllen" in script
    assert "Warenkorb wird gefüllt" in script
    assert "Korb geprüft:" in script
    assert "Nochmal versuchen" in script
    assert "Kopieren" in script
    assert "DevTools öffnen (F12)" in script
    assert "Die Session ist flüchtig" in script
    # Kein "alle Shops füllen" - bewusst pro Shop einzeln.
    assert "alle Shops" not in script
    # Geprüft-aber-nicht-unterstützt ist ein Ergebnis, kein Fehler.
    assert 'class="note cartoff"' in script
    assert ".cartoff { color:var(--muted); }" in css
    assert "cartdiff" in script and "--danger" in css


def test_one_click_handover_sits_above_an_intact_copy_flow():
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")

    # Ein-Klick erscheint nur nach dem Bereitschafts-Signal der Erweiterung.
    assert "beschaffung/extension-ready" in script
    assert "if (!state.extension)" in script
    assert "Im Browser übernehmen" in script
    assert "Übernommen ✓ — Tab geöffnet" in script
    assert "Erweiterung v" in script
    # Herkunftsprüfung auch auf der Seite.
    assert "event.source === window && event.origin === window.location.origin" in script
    # Der Kopierflow bleibt vollständig bestehen - er ist der Fallback.
    assert "Kopieren" in script
    assert "DevTools öffnen (F12)" in script
    assert "Die Session ist flüchtig" in script
    # Cookie-Werte werden nie geloggt.
    assert "console.log" not in script


def test_the_shops_page_lets_linus_add_and_edit_delivery_addresses():
    client, repository = client_and_repo()

    page = client.get("/shops").text

    # Keine fest verdrahteten Ziele - die Liste kommt aus den Daten.
    assert "Lieferadresse hinzufügen" in page
    assert "Zuhause (CH)" in page and "Postfach (DE)" in page
    assert 'action="/lieferziele"' in page
    assert 'action="/lieferziele/1"' in page
    # Die Semantik steht auf der Seite, nicht nur im Auftrag.
    assert "Shopherkunft und Lieferziel sind getrennte Fakten" in page
    assert "aus jedem Land stammen" in page

    angelegt = client.post(
        "/lieferziele",
        data={"name": "Postfach Süd", "adresse": "Weg 3", "land": "de",
              "aufschlag_chf": "20", "zuschlag_tage": "2"},
        follow_redirects=False,
    )

    assert angelegt.status_code == 303
    geschrieben = repository.lieferziel_writes[-1]
    assert geschrieben["land"] == "DE"
    assert geschrieben["waehrung"] == "EUR"   # folgt dem Land
    assert geschrieben["zuschlag_tage"] == 2


def test_an_address_in_a_country_without_a_known_currency_is_rejected():
    client, _ = client_and_repo()

    response = client.post(
        "/lieferziele",
        data={"name": "Irgendwo", "adresse": "Weg 1", "land": "ZZ"},
    )

    assert response.status_code == 422
    assert "Währung" in response.json()["detail"]


def test_shop_profile_api_accepts_unknown_shipping_and_keeps_currency():
    client, repository = client_and_repo()

    response = client.put("/api/shops/1/profile", json={
        "versand_chf": None,
        "gratis_ab_chf": None,
        "mindestbestellwert_chf": None,
        "lieferzeit_default_tage": 3,
        "profil_quelle_url": "https://shop.example.ch/shipping",
        "versand_text": "Versandkosten erst im Checkout",
        "waehrung": "USD",
    })

    assert response.status_code == 200
    assert repository.shop_profile_writes[0]["versand_chf"] is None
    assert repository.shop_profile_writes[0]["versand_waehrung"] == "USD"


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
    assert "Shopherkunft und Lieferziel sind getrennte Fakten" in shops.text
    assert updated.status_code == 303
    assert repository.shop_status == (1, "bestaetigt")


def test_the_browser_shows_currency_evidence_pickup_and_no_quick_action_for_only_ch():
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")
    css = Path("static/app.css").read_text(encoding="utf-8")
    page = client_and_repo()[0].get("/jobs/7").text

    # Fremdwährung: Original, Umrechnung und Beleg - wie bei Lieferzeit-Texten.
    assert "waehrung_fremd" in script
    assert "preis_original_text" in script
    assert "waehrung_beleg" in script
    assert '<div class="fx">' in script and ".fx {" in css
    assert 'id="currency-toggle"' in page
    assert 'job-matrix.js?v=7' in page
    assert "currencyMode" in script
    assert "versandOriginalText" in script
    assert "versand_kurs" in script
    assert "versand_unbekannt" in script

    # Abholung sichtbar am Shop und als eigene Zeile im Total.
    assert "Abholung: ${esc(shop.lieferziel_name)}" in script
    assert "pickup-row" in script and "aufschlaege" in script
    assert "chip.pickup" in css

    # Wertfreigrenzen-Indikator wird angezeigt, nicht gerechnet.
    assert "einfuhr" in script
    assert "ueber_freigrenze" in script

    # «Nur Schweiz» zeigt die offenen Zeilen, aber ohne Quick-Action.
    assert 'includes("only_ch")' in script
    assert "deckt ${esc(missing)} nicht ab" in script


def test_the_scenario_payload_carries_the_pickup_and_import_fields():
    client, _ = client_and_repo()

    matrix = client.post("/api/jobs/7/scenarios", json={"tempo": 0.5}).json()
    plan = matrix["scenarios"][0]

    # Rein schweizerischer Testbestand: Felder da, aber leer.
    assert plan["aufschlaege"] == []
    assert plan["aufschlag_chf"] == "0.00"
    assert plan["einfuhr"] == []
    assert plan["enthaelt_abholung"] is False
    assert plan["deckt_nicht_ab"] is None
    assert plan["lines"][0]["waehrung"] == "CHF"
    assert plan["lines"][0]["waehrung_fremd"] is False
    assert plan["lines"][0]["abholung"] is False
    # Die Kandidatenzeilen der Matrix tragen dieselben Felder - dort schaut man
    # beim Vergleichen hin, und nur dort rendert die Fremdwährungszeile.
    kandidat = matrix["lines"][0]["candidates"][0]
    for feld in ("waehrung", "waehrung_fremd", "waehrung_beleg", "abholung", "lieferziel_name"):
        assert feld in kandidat, f"{feld} fehlt am Matrix-Kandidaten"


def test_the_job_page_offers_a_price_check_and_the_capture_widgets():
    script = Path("static/job-matrix.js").read_text(encoding="utf-8")
    css = Path("static/app.css").read_text(encoding="utf-8")
    page = client_and_repo()[0].get("/jobs/7").text

    # Serverseitig sichtbar: der Knopf und der Platz für seinen Fortschritt.
    assert 'id="check-prices"' in page
    assert "Preise prüfen" in page
    assert 'id="refresh-view"' in page

    # Erfassung per URL, ergebnisweise, mit dem Weg von Hand daneben.
    assert "Angebot per URL hinzufügen" in script
    assert "data-add-urls" in script and "data-add-run" in script
    assert "/offers/fetch" in script
    assert "von Hand nachtragen" in script
    assert "wörtlich von der Seite" in script
    assert ".addbox" in css and ".manualform" in css

    # Das Schwache markieren: «ungeprüft» ist ein Badge, «via Adapter» bleibt still.
    assert "erfasstBadge" in script
    assert "ungeprüft" in script
    assert "chip.unverified" in css and ".via {" in css

    # Auffrischen: nacheinander, abbrechbar, mit Übersprungen-Zähler.
    assert "/refreshable" in script and "/refresh`" in script
    assert 'id="check-cancel"' in script
    assert "ohne Adapter übersprungen" in script
    assert "geprüft — noch" in script
    assert ".refreshrow" in css


def test_manual_offer_e2e_uses_a_disposable_job_and_never_reaches_a_shop():
    script = Path("tests/e2e/manual_offer_click.mjs").read_text(encoding="utf-8")

    assert "/api/e2e/jobs" in script
    assert "method: 'DELETE'" in script
    assert "'X-E2E-Marker'" in script
    # Der Engine-Versuch scheitert an AdapterFehlt, also vor jedem Netzzugriff -
    # deshalb ist genau eine 422 das erwartete Ergebnis, kein Fehlschlag.
    assert "fetchResponses, [422]" in script
    assert "recordResponses, [200]" in script
    assert "engineError" in script
    assert "manualFormPrefilled" in script
    assert "unverifiedBadge" in script
    assert "badgePersisted" in script
    # Der Auffrischlauf ginge ans echte Netz und wird hier nicht geklickt.
    assert "priceCheckPresent" in script
    assert "#check-prices').click" not in script
