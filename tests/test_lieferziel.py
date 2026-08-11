"""Lieferziele als Daten.

Shop-Herkunftsland, Angebotswährung und beliefertes Ziel sind unabhängige
Fakten. Entscheidend ist die belegte Lieferung an eine konfigurierte Adresse.
"""

from decimal import Decimal

import pytest

from app.procurement import ProcurementService, ValidationError

from tests.test_procurement import FakeProcurementRepository


def service():
    repository = FakeProcurementRepository()
    return ProcurementService(repository), repository


# ---------------------------------------------------------------------------
# Anlegen
# ---------------------------------------------------------------------------

def test_the_currency_follows_the_country_and_stays_overridable():
    procurement, _ = service()

    oesterreich = procurement.record_lieferziel("Wien", "Ringstrasse 1", "AT")
    speziell = procurement.record_lieferziel(
        "Sonderfall", "Musterweg 2", "AT", waehrung="chf"
    )

    assert oesterreich["waehrung"] == "EUR"
    assert speziell["waehrung"] == "CHF"


def test_a_country_without_a_known_currency_must_state_one():
    procurement, _ = service()

    with pytest.raises(ValidationError, match="keine Währung hinterlegt"):
        procurement.record_lieferziel("Irgendwo", "Strasse 1", "ZZ")

    gesetzt = procurement.record_lieferziel("Irgendwo", "Strasse 1", "ZZ", waehrung="EUR")
    assert gesetzt["waehrung"] == "EUR"


def test_a_delivery_target_needs_a_name_an_address_and_a_country_code():
    procurement, _ = service()

    with pytest.raises(ValidationError, match="Name"):
        procurement.record_lieferziel("  ", "Strasse 1", "DE")
    with pytest.raises(ValidationError, match="Adresse"):
        procurement.record_lieferziel("Postfach", "", "DE")
    with pytest.raises(ValidationError, match="Ländercode"):
        procurement.record_lieferziel("Postfach", "Strasse 1", "Deutschland")


def test_surcharges_are_optional_and_default_to_zero():
    procurement, _ = service()

    ziel = procurement.record_lieferziel("Postfach 2", "Grenzweg 9", "DE")

    assert ziel["aufschlag_chf"] == Decimal("0")
    assert ziel["zuschlag_tage"] == 0


def test_a_negative_surcharge_is_refused():
    procurement, _ = service()

    with pytest.raises(ValidationError):
        procurement.record_lieferziel("Postfach 3", "Weg 1", "DE", aufschlag_chf=-5)
    with pytest.raises(ValidationError, match="Zuschlag-Tage"):
        procurement.record_lieferziel("Postfach 4", "Weg 1", "DE", zuschlag_tage=-1)


def test_the_home_address_is_marked_as_such():
    procurement, _ = service()

    ziele = {ziel["name"]: ziel for ziel in procurement.list_lieferziele()}

    assert ziele["Zuhause (CH)"]["ist_heimat"] is True
    assert ziele["Zuhause (CH)"]["aufschlag_chf"] == Decimal("0")
    assert ziele["Postfach (DE)"]["ist_heimat"] is False


# ---------------------------------------------------------------------------
# Zuordnung beim Shop
# ---------------------------------------------------------------------------

def anlegen(procurement, name, url, land, **kwargs):
    return procurement.record_shop(
        name, url, land, 5.0, None, None, 3,
        f"{url}versand", "Versand 5", **kwargs,
    )


def test_several_targets_in_one_country_demand_an_explicit_choice():
    procurement, _ = service()
    procurement.record_lieferziel("Postfach Nord", "Nordweg 1", "DE")

    with pytest.raises(ValidationError, match="Mehrere Lieferadressen in DE"):
        anlegen(procurement, "Reichelt", "https://www.reichelt.de/", "DE")

    gewaehlt = anlegen(
        procurement, "Reichelt", "https://www.reichelt.de/", "DE", lieferziel_id=2
    )
    assert gewaehlt["lieferziel_id"] == 2


def test_an_explicit_target_may_differ_from_the_shop_country():
    procurement, _ = service()

    shop = anlegen(
        procurement, "SparkFun", "https://www.sparkfun.com/", "US", lieferziel_id=1
    )

    assert shop["land"] == "US"
    assert shop["lieferziel_id"] == 1


def test_an_unknown_target_is_refused():
    procurement, _ = service()

    with pytest.raises(ValidationError, match="Lieferadresse 99 ist unbekannt"):
        anlegen(procurement, "Reichelt", "https://www.reichelt.de/", "DE", lieferziel_id=99)


# ---------------------------------------------------------------------------
# Angebotswährung ist unabhängig von Shopland und Lieferziel
# ---------------------------------------------------------------------------

def euro_ready():
    procurement, repository = service()
    repository.kurse["EUR"] = {
        "waehrung": "EUR", "kurs": "0.94",
        "geholt_am": ProcurementService._heute(),
        "quelle_url": "https://api.frankfurter.app/latest?from=EUR&to=CHF",
    }
    return procurement, repository


def test_a_euro_offer_at_a_shop_delivering_to_switzerland_is_converted():
    procurement, _ = euro_ready()

    gespeichert = procurement.record_offer(
        10, 1, "Servo", "https://shop.example.ch/servo", "7.99",
        lieferzeit_text="2 Tage", waehrung="EUR",
    )

    assert gespeichert["waehrung"] == "EUR"
    assert gespeichert["preis_chf"] == Decimal("7.51")


def test_a_chf_offer_at_a_shop_delivering_to_germany_stays_chf():
    procurement, _ = euro_ready()

    gespeichert = procurement.record_offer(
        10, 2, "Servo", "https://www.reichelt.de/servo", "7.99",
        lieferzeit_text="2 Tage", waehrung="CHF",
    )

    assert gespeichert["waehrung"] == "CHF"
    assert gespeichert["preis_chf"] == Decimal("7.99")


def test_both_target_currencies_still_go_through():
    procurement, _ = euro_ready()

    schweiz = procurement.record_offer(
        10, 1, "Servo", "https://shop.example.ch/servo", "12.50", lieferzeit_text="2 Tage",
    )
    deutschland = procurement.record_offer(
        10, 2, "Servo", "https://www.reichelt.de/servo", "7.99",
        lieferzeit_text="2 Tage", waehrung="EUR",
    )

    assert schweiz["waehrung"] == "CHF"
    assert schweiz["preis_chf"] == Decimal("12.50")
    assert deutschland["waehrung"] == "EUR"
    assert deutschland["preis_chf"] == Decimal("7.51")
