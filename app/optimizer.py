"""Reiner Bestelloptimierer.

Tempo bewertet den langsamsten Liefertag mit 15 CHF pro Tag bei tempo=1.
Die Konstante bildet den expliziten Preis-vs.-Geschwindigkeit-Kompromiss ab.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations


TEMPO_COST_PER_DAY_CHF = Decimal("15.00")


@dataclass(frozen=True)
class ShopProfile:
    id: int
    name: str
    versand_chf: Decimal
    gratis_ab_chf: Decimal | None
    mindestbestellwert_chf: Decimal | None
    lieferzeit_default_tage: int


@dataclass(frozen=True)
class Offer:
    id: int
    line_id: int
    shop_id: int
    preis_chf: Decimal
    menge: int
    lieferzeit_tage: int | None = None

    @property
    def positionspreis(self) -> Decimal:
        return self.preis_chf * self.menge


@dataclass(frozen=True)
class OrderVariant:
    shop_ids: tuple[int, ...]
    assignments: dict[int, int]
    subtotals: dict[int, Decimal]
    shipping: dict[int, Decimal]
    total_chf: Decimal
    max_liefertage: int
    score: Decimal


def optimize_orders(
    offers: list[Offer],
    shops: list[ShopProfile],
    tempo: float,
    limit: int = 3,
) -> list[OrderVariant]:
    if not 0 <= tempo <= 1:
        raise ValueError("tempo muss zwischen 0 und 1 liegen")
    if limit < 1:
        raise ValueError("limit muss positiv sein")
    if not offers:
        return []

    shops_by_id = {shop.id: shop for shop in shops}
    offers_by_line: dict[int, list[Offer]] = {}
    for offer in offers:
        if offer.shop_id not in shops_by_id:
            raise ValueError(f"Shop-Profil {offer.shop_id} fehlt")
        if offer.menge < 1 or offer.preis_chf <= 0:
            raise ValueError("Angebote brauchen positive Menge und Preis")
        offers_by_line.setdefault(offer.line_id, []).append(offer)

    variants: list[OrderVariant] = []
    available_shop_ids = sorted({offer.shop_id for offer in offers})
    for shop_count in range(1, min(3, len(available_shop_ids)) + 1):
        for shop_ids in combinations(available_shop_ids, shop_count):
            selected = set(shop_ids)
            chosen: dict[int, Offer] = {}
            for line_id, line_offers in offers_by_line.items():
                candidates = [offer for offer in line_offers if offer.shop_id in selected]
                if not candidates:
                    break
                chosen[line_id] = min(
                    candidates,
                    key=lambda offer: (offer.positionspreis, offer.shop_id, offer.id),
                )
            if len(chosen) != len(offers_by_line):
                continue
            used_shop_ids = {offer.shop_id for offer in chosen.values()}
            if used_shop_ids != selected:
                continue

            subtotals = {shop_id: Decimal("0.00") for shop_id in shop_ids}
            for offer in chosen.values():
                subtotals[offer.shop_id] += offer.positionspreis
            if any(
                shops_by_id[shop_id].mindestbestellwert_chf is not None
                and subtotals[shop_id] < shops_by_id[shop_id].mindestbestellwert_chf
                for shop_id in shop_ids
            ):
                continue

            shipping: dict[int, Decimal] = {}
            for shop_id in shop_ids:
                shop = shops_by_id[shop_id]
                shipping[shop_id] = (
                    Decimal("0.00")
                    if shop.gratis_ab_chf is not None
                    and subtotals[shop_id] >= shop.gratis_ab_chf
                    else shop.versand_chf
                )
            total = sum(subtotals.values(), Decimal("0.00")) + sum(
                shipping.values(), Decimal("0.00")
            )
            max_days = max(
                offer.lieferzeit_tage
                or shops_by_id[offer.shop_id].lieferzeit_default_tage
                for offer in chosen.values()
            )
            score = total + Decimal(str(tempo)) * TEMPO_COST_PER_DAY_CHF * max_days
            variants.append(
                OrderVariant(
                    shop_ids=shop_ids,
                    assignments={line_id: offer.id for line_id, offer in chosen.items()},
                    subtotals=subtotals,
                    shipping=shipping,
                    total_chf=total,
                    max_liefertage=max_days,
                    score=score,
                )
            )

    variants.sort(key=lambda variant: (variant.score, variant.total_chf, variant.shop_ids))
    return variants[:limit]
