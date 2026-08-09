from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.procurement import ProcurementService, ValidationError


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
                {"id": 31, "line_id": 10, "shop_id": 1, "preis_chf": "10", "menge": 2, "lieferzeit_tage": 2}
            ],
            "shops": [
                {"id": 1, "name": "Servo Shop", "versand_chf": "8", "gratis_ab_chf": None, "mindestbestellwert_chf": None, "lieferzeit_default_tage": 3}
            ],
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


def test_record_shop_accepts_only_ch_and_starts_unverified():
    procurement = service()

    shop = procurement.record_shop(
        "Neu", "https://neu.ch/versand", "CH", 7.9, 100, None, 3
    )

    assert shop["status"] == "ungeprueft"
    assert shop["domain"] == "neu.ch"
    with pytest.raises(ValidationError, match="nur Shops aus der Schweiz"):
        procurement.record_shop("DE", "https://de.example", "DE", 5, None, None, 3)


def test_record_offer_validates_price_domain_shop_and_line():
    procurement = service()

    offer = procurement.record_offer(
        10, 1, "MG996R", "https://shop.example.ch/mg996r", 12.5, 2, "lagernd"
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
