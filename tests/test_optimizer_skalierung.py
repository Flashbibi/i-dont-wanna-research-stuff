# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Flashbibi
"""Skalierung der Planaufzählung.

Hintergrund: die Aufzählung lief als vollständiges kartesisches Produkt über
alle Zeilen. Bei Job 10 mit 13 Positionen sind das Millionen Pläne, die alle
gebaut und gehalten wurden - dreimal pro Seitenaufruf. Daran ist CT 104
erstickt. Diese Tests halten fest, dass das nicht zurückkommt und dass die
Ergebnisse dort exakt bleiben, wo die vollständige Aufzählung bezahlbar ist.
"""

from decimal import Decimal
from itertools import product

import pytest

from app.optimizer import (
    KOMBINATIONS_BUDGET,
    Offer,
    ShopProfile,
    _build_variant,
    _complete_variants,
    _delivery_rank,
    _validated_inputs,
    optimize_orders,
)


def shop(shop_id, *, versand="6.90", gratis=None, minimum=None, tage=3):
    return ShopProfile(
        id=shop_id, name=f"S{shop_id}",
        versand_chf=None if versand is None else Decimal(versand),
        gratis_ab_chf=None if gratis is None else Decimal(gratis),
        mindestbestellwert_chf=None if minimum is None else Decimal(minimum),
        lieferzeit_default_tage=tage,
    )


def angebot(offer_id, line_id, shop_id, preis, *, menge=1, tage=2):
    return Offer(
        id=offer_id, line_id=line_id, shop_id=shop_id, preis_chf=Decimal(preis),
        menge=menge, lieferzeit_tage=tage, product_key=f"p{line_id}-{offer_id}",
    )


def brute_force(offers_by_line, shops_by_id, lines, tempo=0.5):
    """Die alte Aufzählung - hier nur noch als Vergleichsmassstab."""
    return [
        variant
        for belegung in product(*(offers_by_line[line_id] for line_id in lines))
        if (variant := _build_variant(dict(zip(lines, belegung)), shops_by_id, tempo))
        is not None
    ]


def aufbau(zeilen, kandidaten, shops, **shop_args):
    profile = [shop(s, **shop_args) for s in range(1, shops + 1)]
    offers, oid = [], 1
    for line_id in range(1, zeilen + 1):
        for k in range(kandidaten):
            offers.append(angebot(oid, line_id, (k % shops) + 1, str(8 + k * 3)))
            oid += 1
    return _validated_inputs(offers, profile)


# ---------------------------------------------------------------------------
# Klein bleibt exakt
# ---------------------------------------------------------------------------

def test_a_job_within_the_budget_is_still_enumerated_exactly():
    shops_by_id, offers_by_line = aufbau(6, 3, 5)
    lines = sorted(offers_by_line)
    assert 3 ** 6 <= KOMBINATIONS_BUDGET

    jetzt = _complete_variants(offers_by_line, shops_by_id, lines, 0.5)
    frueher = brute_force(offers_by_line, shops_by_id, lines)

    # Gleiche Pläne, nicht nur gleiche Optima.
    assert len(jetzt) == len(frueher)
    assert {tuple(sorted(v.assignments.items())) for v in jetzt} == {
        tuple(sorted(v.assignments.items())) for v in frueher
    }


def test_the_optima_match_the_full_enumeration_within_the_budget():
    shops_by_id, offers_by_line = aufbau(5, 4, 4, gratis="60", minimum="20")
    lines = sorted(offers_by_line)

    jetzt = _complete_variants(offers_by_line, shops_by_id, lines, 0.5)
    frueher = brute_force(offers_by_line, shops_by_id, lines)

    for name, key in {
        "Total": lambda v: v.total_chf,
        "Lieferzeit": lambda v: _delivery_rank(v.max_liefertage),
        "Score": lambda v: v.score,
    }.items():
        assert min(key(v) for v in jetzt) == min(key(v) for v in frueher), name


# ---------------------------------------------------------------------------
# Gross explodiert nicht mehr
# ---------------------------------------------------------------------------

def test_a_thirteen_line_job_no_longer_enumerates_millions():
    """Job 10: 13 Positionen. Früher 1,6 Millionen Pläne, drei Mal pro Aufruf."""
    shops_by_id, offers_by_line = aufbau(13, 3, 13)
    lines = sorted(offers_by_line)
    assert 3 ** 13 > KOMBINATIONS_BUDGET, "dieser Fall muss über dem Budget liegen"

    variants = _complete_variants(offers_by_line, shops_by_id, lines, 0.5)

    assert variants, "es muss weiterhin Pläne geben"
    # Der Beweis, dass nicht mehr vollständig aufgezählt wird.
    assert len(variants) < 1_000, f"{len(variants)} Pläne gebaut - das ist zu viel"


@pytest.mark.parametrize("zeilen,kandidaten", [(20, 5), (60, 4), (200, 3)])
def test_even_the_largest_allowed_job_stays_computable(zeilen, kandidaten):
    shops_by_id, offers_by_line = aufbau(zeilen, kandidaten, 8)
    lines = sorted(offers_by_line)

    variants = _complete_variants(offers_by_line, shops_by_id, lines, 0.5)

    assert variants
    assert len(variants) < 1_000


# ---------------------------------------------------------------------------
# Die Schwellen müssen auch im grossen Fall greifen
# ---------------------------------------------------------------------------

def test_a_minimum_order_value_is_repaired_instead_of_dropping_the_plan():
    """Die billigste Zuordnung kann unter dem Mindestbestellwert liegen.

    Früher fiel der Plan dann einfach weg, obwohl es ihn zu einem etwas
    höheren Warenwert gibt. Der teurere Einkauf muss gefunden werden.
    """
    shops_by_id, offers_by_line = _validated_inputs(
        [angebot(oid, line_id, 1, preis)
         for oid, (line_id, preis) in enumerate(
             [(l, p) for l in range(1, 13) for p in ("2.00", "9.00")], start=1)],
        [shop(1, minimum="60", versand="5.00")],
    )
    lines = sorted(offers_by_line)

    variants = _complete_variants(offers_by_line, shops_by_id, lines, 0.5)

    assert variants, "der Plan existiert - er darf nicht verschwinden"
    assert all(v.subtotals[1] >= Decimal("60") for v in variants)


def test_free_shipping_is_reached_by_buying_up_in_the_large_case():
    """Teurer einkaufen kann billiger sein - auch jenseits des Budgets.

    Rechnung: 13 Zeilen. Bei Shop 1 kostet die billigste Ware 13x3.00 = 39.00
    plus 15.00 Versand = 54.00. Wer auf die Gratisgrenze von 50 aufstockt,
    zahlt 51.00 und keinen Versand. Shop 2 liegt bei 13x4.00 + 9.90 = 61.90.
    """
    guenstig = shop(1, versand="15.00", gratis="50")
    teuer = shop(2, versand="9.90", gratis=None)
    offers = []
    oid = 1
    for line_id in range(1, 14):
        offers.append(angebot(oid, line_id, 1, "3.00")); oid += 1
        offers.append(angebot(oid, line_id, 1, "6.00")); oid += 1
        offers.append(angebot(oid, line_id, 2, "4.00")); oid += 1

    beste = optimize_orders(offers, [guenstig, teuer], tempo=0.0, limit=5)[0]

    assert beste.shop_ids == (1,)
    assert beste.subtotals[1] >= Decimal("50")
    assert beste.shipping[1] == Decimal("0.00")
    # Billiger als sowohl «billigste Ware plus Versand» als auch Shop 2.
    assert beste.total_chf < Decimal("54.00")
    assert beste.total_chf < Decimal("61.90")
