# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
from fastapi.testclient import TestClient

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
        return [{
            "id": 4, "bezeichnung": "Servo", "menge": 3, "einheit": "Stk",
            "artikelnummer": "MG90S", "shop_name": "Servo Shop",
            "produkt_url": "https://shop.example/servo",
            "aktualisiert_am": "2026-08-20T10:00:00+00:00",
        }]

    def get_stock_bewegungen(self, limit=20):
        return [{
            "bezeichnung": "Servo", "delta": 3, "grund": "zugang_lieferung",
            "kommentar": None, "erstellt_am": "2026-08-20T10:00:00+00:00",
        }]


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
    for text in ("Servo", "MG90S", "Servo Shop", "3 Stk", "Letzte Bewegungen", "+3", "vor"):
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
