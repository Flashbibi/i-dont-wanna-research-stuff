"""Optimierer mit Lieferzielen.

Die Leitidee: verglichen wird nie der Artikelpreis, sondern der ehrliche
Endpreis pro Lieferziel - inklusive Abholaufwand und Wartezeit.
"""

from decimal import Decimal

from app.optimizer import (
    Offer,
    ShopProfile,
    filter_dominated_variants,
    optimize_orders,
    plan_scenarios,
)


def heimat(shop_id=1, name="Bastelgarage", versand="0.00", tage=2):
    return ShopProfile(
        id=shop_id, name=name, versand_chf=Decimal(versand), gratis_ab_chf=None,
        mindestbestellwert_chf=None, lieferzeit_default_tage=tage,
        lieferziel_id=1, lieferziel_name="Zuhause (CH)",
        aufschlag_chf=Decimal("0.00"), zuschlag_tage=0, ist_heimat=True,
    )


def deutsch(shop_id, name, *, versand="0.00", tage=2, aufschlag="25.00", zuschlag=3):
    return ShopProfile(
        id=shop_id, name=name, versand_chf=Decimal(versand), gratis_ab_chf=None,
        mindestbestellwert_chf=None, lieferzeit_default_tage=tage,
        lieferziel_id=2, lieferziel_name="Postfach (DE)",
        aufschlag_chf=Decimal(aufschlag), zuschlag_tage=zuschlag, ist_heimat=False,
    )


def angebot(offer_id, line_id, shop_id, preis, *, menge=1, tage=None):
    return Offer(
        id=offer_id, line_id=line_id, shop_id=shop_id, preis_chf=Decimal(preis),
        menge=menge, lieferzeit_tage=tage, product_key=f"p{line_id}",
    )


# ---------------------------------------------------------------------------
# Aufschlag einmal pro Ziel, nicht pro Shop
# ---------------------------------------------------------------------------

def test_two_german_shops_in_one_plan_cost_exactly_one_pickup():
    """Eine Abholfahrt, egal wie viele DE-Shops im Plan liegen."""
    shops = [deutsch(2, "Reichelt"), deutsch(3, "Pollin")]
    offers = [
        angebot(1, 10, 2, "10.00", tage=2),
        angebot(2, 11, 3, "20.00", tage=2),
    ]

    variante = optimize_orders(offers, shops, tempo=0.0)[0]

    assert variante.shop_ids == (2, 3)
    # 10 + 20 Ware, ein einziger Aufschlag von 25 - nicht zwei.
    assert variante.aufschlag_chf == Decimal("25.00")
    assert variante.total_chf == Decimal("55.00")
    assert [name for _, name, _ in variante.aufschlaege] == ["Postfach (DE)"]


def test_two_targets_in_one_plan_each_cost_their_own_pickup():
    shops = [heimat(), deutsch(2, "Reichelt")]
    offers = [angebot(1, 10, 1, "10.00", tage=2), angebot(2, 11, 2, "20.00", tage=2)]

    variante = optimize_orders(offers, shops, tempo=0.0)[0]

    # Die Heimat kostet nichts, das DE-Ziel einmal.
    assert variante.aufschlag_chf == Decimal("25.00")
    assert variante.total_chf == Decimal("55.00")


def test_a_pure_home_plan_carries_no_surcharge_line():
    shops = [heimat()]
    offers = [angebot(1, 10, 1, "10.00", tage=2)]

    variante = optimize_orders(offers, shops, tempo=0.0)[0]

    assert variante.aufschlaege == ()
    assert variante.aufschlag_chf == Decimal("0.00")
    assert variante.total_chf == Decimal("10.00")


def test_the_waiting_time_is_added_to_every_shop_of_that_target():
    shops = [heimat(tage=2), deutsch(2, "Reichelt", tage=2, zuschlag=3)]
    offers = [angebot(1, 10, 1, "10.00", tage=2), angebot(2, 11, 2, "20.00", tage=2)]

    variante = optimize_orders(offers, shops, tempo=0.0)[0]

    # CH bleibt bei 2, DE wird 2 + 3 = 5; das Maximum bestimmt den Plan.
    assert variante.max_liefertage == 5


# ---------------------------------------------------------------------------
# Dominanzfilter arbeitet auf Totalen NACH Aufschlag
# ---------------------------------------------------------------------------

def test_a_german_plan_cheap_before_pickup_but_worse_after_is_filtered_out():
    """Der Kern gegen «billig gewinnt immer».

    Vor Aufschlag ist der DE-Plan billiger (30 statt 40). Nach Aufschlag ist er
    teurer (55 statt 40) UND langsamer (5 statt 2 Tage) - also dominiert und
    damit unsichtbar.
    """
    shops = [heimat(tage=2), deutsch(2, "Reichelt", tage=2, aufschlag="25.00", zuschlag=3)]
    schweiz = optimize_orders(
        [angebot(1, 10, 1, "40.00", tage=2)], [shops[0]], tempo=0.0
    )[0]
    deutschland = optimize_orders(
        [angebot(2, 10, 2, "30.00", tage=2)], [shops[1]], tempo=0.0
    )[0]

    # Vor Aufschlag waere Deutschland vorn.
    assert deutschland.total_chf - deutschland.aufschlag_chf < schweiz.total_chf
    # Nach Aufschlag ist es teurer und langsamer.
    assert deutschland.total_chf == Decimal("55.00")
    assert deutschland.max_liefertage == 5
    assert schweiz.total_chf == Decimal("40.00")
    assert schweiz.max_liefertage == 2

    sichtbar = filter_dominated_variants([schweiz, deutschland])

    assert schweiz in sichtbar
    assert deutschland not in sichtbar


def test_a_german_plan_that_stays_cheaper_after_pickup_survives():
    shops = [heimat(tage=2), deutsch(2, "Reichelt", tage=2, aufschlag="25.00", zuschlag=3)]
    schweiz = optimize_orders([angebot(1, 10, 1, "200.00", tage=2)], [shops[0]], tempo=0.0)[0]
    deutschland = optimize_orders([angebot(2, 10, 2, "100.00", tage=2)], [shops[1]], tempo=0.0)[0]

    sichtbar = filter_dominated_variants([schweiz, deutschland])

    # 125 statt 200 - der Umweg lohnt sich und bleibt sichtbar, obwohl langsamer.
    assert deutschland.total_chf == Decimal("125.00")
    assert deutschland in sichtbar
    assert schweiz in sichtbar


# ---------------------------------------------------------------------------
# Nur-Schweiz-Preset
# ---------------------------------------------------------------------------

def test_only_ch_matches_the_overall_optimum_when_there_is_no_foreign_offer():
    shops = [heimat()]
    offers = [angebot(1, 10, 1, "10.00", tage=2)]

    presets = plan_scenarios(offers, shops, required_line_ids=[10])

    assert "only_ch" in presets
    assert presets["only_ch"].assignments == presets["cheapest"].assignments


def test_only_ch_becomes_its_own_plan_once_a_foreign_offer_is_cheaper():
    shops = [heimat(tage=2), deutsch(2, "Reichelt", tage=2, aufschlag="5.00", zuschlag=1)]
    offers = [
        angebot(1, 10, 1, "40.00", tage=2),
        angebot(2, 10, 2, "10.00", tage=2),
    ]

    presets = plan_scenarios(offers, shops, required_line_ids=[10])

    # Guenstigster Plan nutzt Deutschland, Nur-Schweiz bleibt bei der Heimat.
    assert presets["cheapest"].shop_ids == (2,)
    assert presets["cheapest"].total_chf == Decimal("15.00")
    assert presets["only_ch"].shop_ids == (1,)
    assert presets["only_ch"].total_chf == Decimal("40.00")
    assert presets["only_ch"].aufschlaege == ()


def test_only_ch_is_absent_when_the_home_market_cannot_cover_every_line():
    shops = [heimat(), deutsch(2, "Reichelt")]
    offers = [
        angebot(1, 10, 1, "10.00", tage=2),
        angebot(2, 11, 2, "20.00", tage=2),   # Zeile 11 gibt es nur in DE
    ]

    presets = plan_scenarios(offers, shops, required_line_ids=[10, 11])

    assert "only_ch" not in presets


# ---------------------------------------------------------------------------
# Wertfreigrenzen-Indikator (reine Anzeige)
# ---------------------------------------------------------------------------

def plan_mit_de(preis):
    """Ein Plan mit genau einer DE-Position, ueber den Service angereichert."""
    from app.procurement import ProcurementService

    data = {
        "offers": [{
            "id": 1, "line_id": 10, "shop_id": 2, "preis_chf": preis, "menge": 1,
            "lieferzeit_tage": 2, "lieferzeit_text": "2 Tage", "lager_text": None,
            "produktname": "Teil", "produkt_url": "https://www.reichelt.de/teil",
            "quelle_url": "https://www.reichelt.de/teil", "suchtext": "Teil",
            "position": 1, "override_status": None,
        }],
        "shops": [{
            "id": 2, "name": "Reichelt", "url": "https://www.reichelt.de",
            "versand_chf": "0.00", "gratis_ab_chf": None,
            "mindestbestellwert_chf": None, "lieferzeit_default_tage": 2,
            "lieferziel_id": 2, "lieferziel_name": "Postfach (DE)",
            "lieferziel_land": "DE", "lieferziel_aufschlag_chf": "25.00",
            "lieferziel_zuschlag_tage": 3,
        }],
        "required_line_ids": [10],
        "lines": [{"id": 10, "position": 1, "suchtext": "Teil", "menge": 1}],
        "selected_assignments": None,
    }
    offers, shops = ProcurementService._optimizer_objects(data)
    variante = optimize_orders(offers, shops, tempo=0.0)[0]
    return ProcurementService._enrich_variant(
        ProcurementService._serialize_variant(variante), data
    )


def test_a_plan_below_the_allowance_says_so_without_touching_the_total():
    plan = plan_mit_de("100.00")

    eintrag = plan["einfuhr"][0]
    assert eintrag["netto_ca_chf"] == "84.03"      # 100 / 1.19
    assert eintrag["ueber_freigrenze"] is False
    assert "unter der Wertfreigrenze" in eintrag["text"]
    # Der Indikator ist Anzeige - das Total kennt nur Ware und Aufschlag.
    assert plan["total_chf"] == "125.00"
    assert plan["enthaelt_abholung"] is True


def test_a_plan_above_the_allowance_names_the_import_tax_without_computing_it():
    plan = plan_mit_de("200.00")

    eintrag = plan["einfuhr"][0]
    assert eintrag["netto_ca_chf"] == "168.07"     # 200 / 1.19
    assert eintrag["ueber_freigrenze"] is True
    assert "über der Wertfreigrenze" in eintrag["text"]
    assert "8.1 % MwSt auf den Gesamtwert" in eintrag["text"]
    # Keine Steuer im Total: 200 Ware + 25 Abholung, sonst nichts.
    assert plan["total_chf"] == "225.00"


def test_a_pure_home_plan_has_no_import_indicator_at_all():
    from app.procurement import ProcurementService

    data = {
        "offers": [{
            "id": 1, "line_id": 10, "shop_id": 1, "preis_chf": "500.00", "menge": 1,
            "lieferzeit_tage": 2, "lieferzeit_text": "2 Tage", "lager_text": None,
            "produktname": "Teil", "produkt_url": "https://shop.ch/teil",
            "quelle_url": "https://shop.ch/teil", "suchtext": "Teil",
            "position": 1, "override_status": None,
        }],
        "shops": [{
            "id": 1, "name": "Bastelgarage", "url": "https://shop.ch",
            "versand_chf": "0.00", "gratis_ab_chf": None,
            "mindestbestellwert_chf": None, "lieferzeit_default_tage": 2,
            "lieferziel_id": 1, "lieferziel_name": "Zuhause (CH)",
            "lieferziel_land": "CH", "lieferziel_aufschlag_chf": "0.00",
            "lieferziel_zuschlag_tage": 0,
        }],
        "required_line_ids": [10],
        "lines": [{"id": 10, "position": 1, "suchtext": "Teil", "menge": 1}],
        "selected_assignments": None,
    }
    offers, shops = ProcurementService._optimizer_objects(data)
    variante = optimize_orders(offers, shops, tempo=0.0)[0]
    plan = ProcurementService._enrich_variant(
        ProcurementService._serialize_variant(variante), data
    )

    assert plan["einfuhr"] == []
    assert plan["enthaelt_abholung"] is False
