from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.procurement import ProcurementService, ValidationError, parse_delivery_upper_days


class FakeProcurementRepository:
    def __init__(self):
        self.shops = {
            1: {
                "id": 1,
                "name": "Servo Shop",
                "url": "https://shop.example.ch",
                "domain": "shop.example.ch",
                "status": "bestaetigt",
            }
        }
        self.lines = {10: {"id": 10, "job_id": 5, "suchtext": "Servo", "menge": 2}}
        self.offers = []
        self.marked = []
        self.purchases = []

    def next_job(self):
        return {"id": 5, "status": "offen", "lines": [self.lines[10]]}

    def check_line(self, line_id):
        if line_id not in self.lines:
            return None
        return {"line": self.lines[line_id], "stock": [], "previous_purchases": [], "cached_offers": []}

    def create_shop(self, **values):
        shop_id = max(self.shops) + 1
        result = {"id": shop_id, "status": "ungeprueft", **values}
        self.shops[shop_id] = result
        return result

    def get_shop(self, shop_id):
        return self.shops.get(shop_id)

    def update_shop_profile(self, shop_id, **values):
        if shop_id not in self.shops:
            raise ValueError("Shop unbekannt")
        self.shops[shop_id].update(values)
        return self.shops[shop_id]

    def get_line(self, line_id):
        return self.lines.get(line_id)

    def create_offer(self, **values):
        result = {"id": len(self.offers) + 20, "gesehen_am": "now", **values}
        self.offers.append(result)
        return result

    def mark_line(self, line_id, status, kommentar):
        self.marked.append((line_id, status, kommentar))
        return {"id": line_id, "status": status, "kommentar": kommentar}

    def optimization_input(self, job_id):
        return {
            "offers": [
                {
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
                    "override_status": None,
                }
            ],
            "shops": [
                {"id": 1, "name": "Servo Shop", "url": "https://shop.example.ch", "versand_chf": "8", "gratis_ab_chf": None, "mindestbestellwert_chf": None, "lieferzeit_default_tage": 3}
            ],
            "required_line_ids": [10],
            "lines": [{"id": 10, "position": 1, "suchtext": "Servo"}],
        }

    def create_purchase(self, job_id, variant, ordered_at, promised_days):
        result = {"id": 90, "job_id": job_id, "variante": variant}
        self.purchases.append((job_id, variant, ordered_at, promised_days))
        return result


def service():
    return ProcurementService(FakeProcurementRepository())


def test_next_job_and_check_line_are_single_repository_calls():
    procurement = service()

    assert procurement.next_job()["id"] == 5
    checked = procurement.check_line(10)

    assert set(checked) == {"line", "stock", "previous_purchases", "cached_offers"}


def test_record_shop_accepts_only_sourced_ch_profile_and_starts_unverified():
    procurement = service()

    shop = procurement.record_shop(
        "Neu",
        "https://neu.ch/",
        "CH",
        7.9,
        100,
        None,
        3,
        "https://neu.ch/versand",
        "Pauschale Versand CHF 7.90; Lieferung in 3 Arbeitstagen",
    )

    assert shop["status"] == "ungeprueft"
    assert shop["domain"] == "neu.ch"
    assert shop["profil_quelle_url"] == "https://neu.ch/versand"
    with pytest.raises(ValidationError, match="nur Shops aus der Schweiz"):
        procurement.record_shop(
            "DE", "https://de.example", "DE", 5, None, None, 3,
            "https://de.example/versand", "Versand 5 EUR",
        )
    with pytest.raises(ValidationError, match="Profil-Quelle"):
        procurement.record_shop(
            "Ohne Quelle", "https://ohne.ch", "CH", 5, None, None, None, "", "Versand CHF 5",
        )
    with pytest.raises(ValidationError, match="Versand-Originaltext"):
        procurement.record_shop(
            "Ohne Text", "https://ohne.ch", "CH", 5, None, None, None,
            "https://ohne.ch/versand", "",
        )


def test_shop_profile_audit_allows_unknown_default_days_but_never_unsourced_values():
    repository = FakeProcurementRepository()
    procurement = ProcurementService(repository)

    result = procurement.record_shop_profile(
        1,
        versand_chf=7.9,
        gratis_ab_chf=None,
        mindestbestellwert_chf=None,
        lieferzeit_default_tage=None,
        profil_quelle_url="https://shop.ch/versand",
        versand_text="B-Post Economy CHF 7.90; keine Lieferdauer genannt",
    )

    assert result["lieferzeit_default_tage"] is None
    assert result["profil_quelle_url"].endswith("/versand")


def test_delivery_text_parser_uses_conservative_range_upper_bound():
    assert parse_delivery_upper_days("3-4 Tage, bei Lieferant an Lager") == 4
    assert parse_delivery_upper_days("Lieferung in 2 Tagen") == 2
    assert parse_delivery_upper_days("sofort lieferbar") is None
    assert parse_delivery_upper_days(None) is None


def test_record_offer_stores_literal_source_text_and_parsed_upper_bound():
    procurement = service()

    offer = procurement.record_offer(
        10,
        1,
        "MG996R",
        "https://shop.example.ch/mg996r",
        12.5,
        "3-4 Tage, bei Lieferant an Lager",
        "Filiale rot; CH-Lieferant an Lager",
    )

    assert offer["quelle_url"] == "https://shop.example.ch/mg996r"
    assert offer["lieferzeit_tage"] == 4
    assert offer["lieferzeit_text"] == "3-4 Tage, bei Lieferant an Lager"
    assert offer["lager_text"] == "Filiale rot; CH-Lieferant an Lager"


def test_record_offer_without_delivery_source_text_keeps_days_empty():
    procurement = service()

    offer = procurement.record_offer(
        10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, None, "lagernd"
    )

    assert offer["lieferzeit_tage"] is None
    assert offer["lieferzeit_text"] is None


def test_record_offer_rejects_parsed_days_without_literal_source_text(monkeypatch):
    procurement = service()
    monkeypatch.setattr("app.procurement.parse_delivery_upper_days", lambda _: 2)

    with pytest.raises(ValidationError, match="Originaltext"):
        procurement.record_offer(
            10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, None, "lagernd"
        )


def test_record_offer_validates_price_domain_shop_and_line():
    procurement = service()

    offer = procurement.record_offer(
        10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, "2 Tage", "lagernd"
    )
    assert offer["quelle_url"] == "https://shop.example.ch/mg996r"

    with pytest.raises(ValidationError, match="Preis"):
        procurement.record_offer(10, 1, "MG996R", "https://shop.example.ch/x", 0)
    with pytest.raises(ValidationError, match="Shop-Domain"):
        procurement.record_offer(10, 1, "MG996R", "https://other.ch/x", 10)
    with pytest.raises(ValidationError, match="unbekannt"):
        procurement.record_offer(10, 999, "MG996R", "https://other.ch/x", 10)

    procurement.repository.shops[1]["status"] = "gesperrt"
    with pytest.raises(ValidationError, match="gesperrt"):
        procurement.record_offer(10, 1, "MG996R", "https://shop.example.ch/x", 10)


def test_mark_line_rejects_unknown_status():
    procurement = service()

    with pytest.raises(ValidationError, match="Status"):
        procurement.mark_line(10, "irgendwas")
    assert procurement.mark_line(10, "nichts_gefunden", "Keine CH-Quelle")["status"] == "nichts_gefunden"


def test_plan_order_calls_pure_optimizer_and_serializes_variant():
    procurement = service()

    variants = procurement.plan_order(5, 0.5)

    assert variants[0]["shop_ids"] == [1]
    assert variants[0]["assignments"] == {"10": 31}
    assert variants[0]["total_chf"] == "28.00"
    assert variants[0]["score"] == "43.000"


def test_plan_scenarios_groups_identical_presets_and_keeps_badges():
    procurement = service()

    result = procurement.plan_scenarios(5)

    assert len(result["scenarios"]) == 1
    scenario = result["scenarios"][0]
    assert scenario["keys"] == ["cheapest", "fastest", "one_shop", "balanced"]
    assert scenario["labels"] == [
        "Am günstigsten",
        "Am schnellsten",
        "Ein Shop",
        "Ausgewogen",
    ]
    assert scenario["contains_estimates"] is False
    assert scenario["fastest_max_exclusively_estimated"] is False
    assert scenario["lines"][0]["lieferzeit_text"] == "2 Tage"
    assert scenario["lines"][0]["produkt_url"].endswith("/mg996r")
    assert scenario["complete"] is True


def test_fastest_marks_when_its_maximum_is_based_only_on_estimates():
    repository = FakeProcurementRepository()
    repository.optimization_input = lambda job_id: {
        **FakeProcurementRepository().optimization_input(job_id),
        "offers": [
            {
                **FakeProcurementRepository().optimization_input(job_id)["offers"][0],
                "lieferzeit_tage": None,
                "lieferzeit_text": None,
            }
        ],
    }
    procurement = ProcurementService(repository)

    scenario = procurement.plan_scenarios(5)["scenarios"][0]

    assert "fastest" in scenario["keys"]
    assert scenario["max_liefertage"] == 3
    assert scenario["fastest_max_exclusively_estimated"] is True


def test_fastest_rejects_cheapest_unknown_delivery_and_serializes_it_as_unknown():
    repository = FakeProcurementRepository()
    base = repository.optimization_input(5)
    cheap_unknown = {
        **base["offers"][0],
        "id": 31,
        "shop_id": 1,
        "preis_chf": "1.00",
        "lieferzeit_tage": None,
        "lieferzeit_text": None,
    }
    known_alternative = {
        **base["offers"][0],
        "id": 32,
        "shop_id": 2,
        "preis_chf": "2.00",
        "lieferzeit_tage": None,
        "lieferzeit_text": None,
        "produktname": "MG996R Servo bekannte Lieferzeit",
        "produkt_url": "https://known.example.ch/mg996r",
    }
    repository.optimization_input = lambda job_id: {
        **base,
        "offers": [cheap_unknown, known_alternative],
        "shops": [
            {
                "id": 1,
                "name": "Unbekannt",
                "url": "https://unknown.example.ch",
                "versand_chf": "0",
                "gratis_ab_chf": None,
                "mindestbestellwert_chf": None,
                "lieferzeit_default_tage": None,
            },
            {
                "id": 2,
                "name": "Bekannt",
                "url": "https://known.example.ch",
                "versand_chf": "0",
                "gratis_ab_chf": None,
                "mindestbestellwert_chf": None,
                "lieferzeit_default_tage": 3,
            },
        ],
    }
    procurement = ProcurementService(repository)

    scenarios = procurement.plan_scenarios(5)["scenarios"]
    cheapest = next(item for item in scenarios if "cheapest" in item["keys"])
    fastest = next(item for item in scenarios if "fastest" in item["keys"])

    assert cheapest["assignments"] == {"10": 31}
    assert cheapest["contains_unknown_delivery"] is True
    assert cheapest["lines"][0]["lieferzeit_tage"] is None
    assert cheapest["lines"][0]["lieferzeit_geschaetzt"] is False
    assert fastest["assignments"] == {"10": 32}
    assert fastest["max_liefertage"] == 3
    assert fastest["contains_unknown_delivery"] is False


def test_plan_order_returns_no_partial_variant_when_a_required_line_lacks_confirmation():
    procurement = service()
    original = procurement.repository.optimization_input

    def incomplete(job_id):
        data = original(job_id)
        data["required_line_ids"] = [10, 11]
        return data

    procurement.repository.optimization_input = incomplete

    assert procurement.plan_order(5, 0.5) == []



def test_record_purchase_requires_valid_timestamp_shop_promises_and_variant():
    procurement = service()
    variant = procurement.plan_order(5, 0)[0]

    purchase = procurement.record_purchase(
        5,
        variant,
        datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
        {"1": 3},
    )

    assert purchase["id"] == 90
    with pytest.raises(ValidationError, match="Liefertage"):
        procurement.record_purchase(5, variant, "2026-08-09T12:00:00+00:00", {})
    with pytest.raises(ValidationError, match="Variante"):
        procurement.record_purchase(5, {"shop_ids": [1]}, "2026-08-09T12:00:00+00:00", {"1": 3})
