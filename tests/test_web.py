from fastapi.testclient import TestClient

from app.jobs import parse_bom
from app.web import create_app


class FakeRepository:
    def __init__(self):
        self.jobs = {}
        self.next_id = 1

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


def test_parse_bom_accepts_optional_quantity_prefix_and_skips_blank_lines():
    lines = parse_bom("2x MG996R Servo\n\n M3 Schrauben\n10 X Kabelbinder")

    assert [(line.position, line.menge, line.suchtext) for line in lines] == [
        (1, 2, "MG996R Servo"),
        (2, 1, "M3 Schrauben"),
        (3, 10, "Kabelbinder"),
    ]


def test_health_reports_migration_state():
    client = TestClient(create_app(FakeRepository(), lambda: 1))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "schema_version": 1}


def test_create_job_persists_lines_without_calculating_live():
    repository = FakeRepository()
    client = TestClient(create_app(repository, lambda: 1))

    response = client.post("/api/jobs", json={"parts": "2x Servo\nKabel"})

    assert response.status_code == 201
    assert response.json() == {"job_id": 1, "status": "offen", "line_count": 2}
    assert repository.jobs[1]["lines"][0]["menge"] == 2
    assert all(line["status"] == "offen" for line in repository.jobs[1]["lines"])


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
