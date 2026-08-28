"""Fremdwährung mit Provenienz.

Kein Test ruft eine echte Kurs-API auf; der Abruf wird durchgehend gestubbt.
"""

import json
from datetime import date
from decimal import Decimal

import pytest

from app.procurement import ProcurementService, ValidationError
from app.waehrung import (
    HOME_CURRENCY,
    Kurs,
    KursError,
    aktueller_kurs,
    fetch_kurs,
    ist_veraltet,
    kurs_badge,
    nach_chf,
)

from tests.test_procurement import FakeProcurementRepository


HEUTE = date(2026, 8, 11)


def antwort(rate="0.94"):
    return lambda url: json.dumps(
        {"amount": 1.0, "base": "EUR", "date": "2026-08-11", "rates": {"CHF": float(rate)}}
    )


# ---------------------------------------------------------------------------
# Abruf und Zwischenspeicher
# ---------------------------------------------------------------------------

def test_the_rate_is_read_from_the_source_with_its_url_as_evidence():
    kurs, quelle = fetch_kurs("EUR", opener=antwort("0.94"))

    assert kurs == Decimal("0.94")
    assert "from=EUR" in quelle and "to=CHF" in quelle


def test_a_source_without_the_target_rate_is_refused():
    with pytest.raises(KursError):
        fetch_kurs("EUR", opener=lambda url: json.dumps({"rates": {"USD": 1.1}}))


def test_todays_stored_rate_is_used_without_fetching():
    repository = FakeProcurementRepository()
    repository.kurse["EUR"] = {
        "waehrung": "EUR", "kurs": "0.95", "geholt_am": HEUTE,
        "quelle_url": "https://api.frankfurter.app/latest?from=EUR&to=CHF",
    }

    def darf_nicht(url):
        raise AssertionError("Es darf kein Abruf stattfinden")

    kurs = aktueller_kurs(repository, "EUR", HEUTE, opener=darf_nicht)

    assert kurs.kurs == Decimal("0.95")
    assert kurs.ersatzweise is False
    assert repository.saved_kurse == []


def test_a_stale_rate_triggers_a_fetch_and_is_stored_with_its_source():
    repository = FakeProcurementRepository()
    repository.kurse["EUR"] = {
        "waehrung": "EUR", "kurs": "0.90", "geholt_am": date(2026, 8, 1),
        "quelle_url": "https://api.frankfurter.app/latest?from=EUR&to=CHF",
    }

    kurs = aktueller_kurs(repository, "EUR", HEUTE, opener=antwort("0.94"))

    assert kurs.kurs == Decimal("0.94")
    assert kurs.geholt_am == HEUTE
    assert repository.saved_kurse[0]["quelle_url"].startswith("https://api.frankfurter.app/")


def test_a_failed_fetch_falls_back_to_the_last_known_rate_and_says_so():
    repository = FakeProcurementRepository()
    repository.kurse["EUR"] = {
        "waehrung": "EUR", "kurs": "0.93", "geholt_am": date(2026, 8, 9),
        "quelle_url": "https://api.frankfurter.app/latest?from=EUR&to=CHF",
    }

    def faellt_aus(url):
        raise TimeoutError("keine Verbindung")

    kurs = aktueller_kurs(repository, "EUR", HEUTE, opener=faellt_aus)

    assert kurs.kurs == Decimal("0.93")
    assert kurs.ersatzweise is True
    assert "Tagesabruf fehlgeschlagen" in kurs_badge(kurs, HEUTE)
    # Ein Ersatzkurs wird nicht als heutiger Kurs weggeschrieben.
    assert repository.saved_kurse == []


def test_no_rate_at_all_is_an_error_not_a_guess():
    repository = FakeProcurementRepository()

    def faellt_aus(url):
        raise TimeoutError("keine Verbindung")

    with pytest.raises(KursError) as error:
        aktueller_kurs(repository, "EUR", HEUTE, opener=faellt_aus)

    assert "kein Preis umgerechnet" in str(error.value).lower() or "Ohne belegten Kurs" in str(error.value)


def test_the_home_currency_needs_no_source():
    kurs = aktueller_kurs(FakeProcurementRepository(), HOME_CURRENCY, HEUTE)

    assert kurs.kurs == Decimal("1")


# ---------------------------------------------------------------------------
# Rundung und Verfall
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "original,kurs,erwartet",
    [
        ("7.99", "0.94", "7.51"),      # 7.5106 -> kaufmännisch auf Rappen
        ("10.00", "0.945", "9.45"),
        ("1.00", "0.9449", "0.94"),
        ("1.00", "0.9450", "0.95"),    # exakt halb -> aufgerundet
    ],
)
def test_conversion_rounds_to_rappen(original, kurs, erwartet):
    assert nach_chf(Decimal(original), Decimal(kurs)) == Decimal(erwartet)


def test_a_rate_older_than_a_week_is_flagged():
    frisch = Kurs("EUR", Decimal("0.94"), date(2026, 8, 5), "https://api.frankfurter.app/x")
    alt = Kurs("EUR", Decimal("0.94"), date(2026, 8, 3), "https://api.frankfurter.app/x")

    assert ist_veraltet(date(2026, 8, 3), HEUTE) is True
    assert ist_veraltet(date(2026, 8, 5), HEUTE) is False
    assert kurs_badge(frisch, HEUTE) is None
    assert "veraltet" in kurs_badge(alt, HEUTE)


def test_the_evidence_line_names_rate_source_and_day():
    kurs = Kurs("EUR", Decimal("0.94"), date(2026, 8, 11), "https://api.frankfurter.app/latest")

    assert kurs.beleg() == "Kurs 0.9400 (EZB, 11.08.)"


# ---------------------------------------------------------------------------
# record_offer: der Server rechnet, nicht der Agent
# ---------------------------------------------------------------------------

def euro_service():
    repository = FakeProcurementRepository()
    repository.kurse["EUR"] = {
        "waehrung": "EUR", "kurs": "0.94", "geholt_am": ProcurementService._heute(),
        "quelle_url": "https://api.frankfurter.app/latest?from=EUR&to=CHF",
    }
    return ProcurementService(repository), repository


def test_record_offer_converts_server_side_and_keeps_the_evidence():
    service, repository = euro_service()

    gespeichert = service.record_offer(
        10, 2, "Servo", "https://www.reichelt.de/servo", "7.99",
        lieferzeit_text="2 Tage", waehrung="EUR",
    )

    # Der Agent liefert 7.99 EUR; den CHF-Wert rechnet der Server.
    assert gespeichert["preis_original"] == Decimal("7.99")
    assert gespeichert["waehrung"] == "EUR"
    assert gespeichert["preis_chf"] == Decimal("7.51")
    assert gespeichert["kurs"] == Decimal("0.94")
    assert gespeichert["kurs_am"] == ProcurementService._heute()
    assert gespeichert["kurs_quelle"].startswith("https://api.frankfurter.app/")


def test_record_offer_in_chf_stays_a_no_op_with_rate_one():
    service, _ = euro_service()

    gespeichert = service.record_offer(
        10, 1, "Servo", "https://shop.example.ch/servo", "12.50",
        lieferzeit_text="2 Tage",
    )

    assert gespeichert["waehrung"] == HOME_CURRENCY
    assert gespeichert["preis_original"] == Decimal("12.50")
    assert gespeichert["preis_chf"] == Decimal("12.50")
    assert gespeichert["kurs"] == Decimal("1")
    assert gespeichert["kurs_quelle"] is None


def test_record_shop_converts_foreign_shipping_and_keeps_original_evidence():
    service, _ = euro_service()

    shop = service.record_shop(
        "Amazon.de", "https://www.amazon.de/", "DE",
        "6.99", "49.00", None, 5,
        "https://www.amazon.de/hilfe/versand", "6,99 EUR; gratis ab 49 EUR",
        lieferziel_id=1, waehrung="EUR",
    )

    assert shop["land"] == "DE"
    assert shop["lieferziel_id"] == 1
    assert shop["versand_original"] == Decimal("6.99")
    assert shop["gratis_ab_original"] == Decimal("49.00")
    assert shop["versand_waehrung"] == "EUR"
    assert shop["versand_chf"] == Decimal("6.57")
    assert shop["gratis_ab_chf"] == Decimal("46.06")
    assert shop["versand_kurs"] == Decimal("0.94")
    assert shop["versand_kurs_quelle"].startswith("https://api.frankfurter.app/")


def test_record_shop_allows_unknown_shipping_without_inventing_a_zero():
    service, _ = euro_service()

    shop = service.record_shop(
        "SparkFun", "https://www.sparkfun.com/", "US",
        None, None, None, 3,
        "https://www.sparkfun.com/support#shipping-policy",
        "Versandkosten erst adressabhängig im Checkout",
        lieferziel_id=1, waehrung="USD",
    )

    assert shop["versand_original"] is None
    assert shop["versand_chf"] is None
    assert shop["versand_waehrung"] == "USD"
    assert shop["versand_kurs"] is None


def test_a_foreign_price_without_any_rate_is_refused_without_writing():
    repository = FakeProcurementRepository()
    service = ProcurementService(repository)

    def faellt_aus(url):
        raise TimeoutError("keine Verbindung")

    import app.waehrung as waehrung_modul

    original = waehrung_modul.fetch_kurs
    waehrung_modul.fetch_kurs = lambda w, opener=None: faellt_aus("x")
    try:
        with pytest.raises(ValidationError):
            service.record_offer(
                10, 2, "Servo", "https://www.reichelt.de/servo", "7.99",
                lieferzeit_text="2 Tage", waehrung="EUR",
            )
    finally:
        waehrung_modul.fetch_kurs = original

    assert repository.offers == []


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_shop_shipping_rejects_non_finite_amounts(value):
    service, _ = euro_service()

    with pytest.raises(ValidationError, match="endliche Zahl"):
        service.record_shop(
            "Kaputt", "https://invalid.example", "CH",
            value, None, None, None,
            "https://invalid.example/shipping", "Unzulässiger Testwert",
            lieferziel_id=1,
        )
