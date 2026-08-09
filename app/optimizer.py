"""Reine, deterministische Bestelloptimierung ohne Datenbank- oder Netzwerkzugriff."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import product


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
    product_key: str | None = None

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
    contains_estimates: bool = False
    missing_line_ids: tuple[int, ...] = ()


def _validated_inputs(
    offers: list[Offer], shops: list[ShopProfile]
) -> tuple[dict[int, ShopProfile], dict[int, list[Offer]]]:
    shops_by_id = {shop.id: shop for shop in shops}
    offers_by_line: dict[int, list[Offer]] = {}
    for offer in offers:
        if offer.shop_id not in shops_by_id:
            raise ValueError(f"Shop-Profil {offer.shop_id} fehlt")
        if offer.menge < 1 or offer.preis_chf <= 0:
            raise ValueError("Angebote brauchen positive Menge und Preis")
        offers_by_line.setdefault(offer.line_id, []).append(offer)
    return shops_by_id, offers_by_line


def _filter_overrides(
    offers_by_line: dict[int, list[Offer]],
    pins: dict[int, int] | None,
    excludes: set[int] | None,
) -> dict[int, list[Offer]]:
    pins = pins or {}
    excludes = excludes or set()
    result: dict[int, list[Offer]] = {}
    for line_id, line_offers in offers_by_line.items():
        candidates = [offer for offer in line_offers if offer.id not in excludes]
        if line_id in pins:
            pinned = next((offer for offer in line_offers if offer.id == pins[line_id]), None)
            if pinned is None:
                raise ValueError(f"Pin {pins[line_id]} passt nicht zu Zeile {line_id}")
            candidates = [
                offer
                for offer in candidates
                if (
                    offer.product_key == pinned.product_key
                    if pinned.product_key is not None
                    else offer.id == pinned.id
                )
            ]
            if not candidates:
                raise ValueError(f"Gepinntes Produkt für Zeile {line_id} ist vollständig ausgeschlossen")
        result[line_id] = candidates
    unknown_pin_lines = set(pins) - set(offers_by_line)
    if unknown_pin_lines:
        raise ValueError(f"Pins für unbekannte Zeilen: {sorted(unknown_pin_lines)}")
    return result


def _build_variant(
    chosen: dict[int, Offer],
    shops_by_id: dict[int, ShopProfile],
    tempo: float,
    missing_line_ids: tuple[int, ...] = (),
) -> OrderVariant | None:
    if not chosen:
        return None
    shop_ids = tuple(sorted({offer.shop_id for offer in chosen.values()}))
    if len(shop_ids) > 3:
        return None
    subtotals = {shop_id: Decimal("0.00") for shop_id in shop_ids}
    for offer in chosen.values():
        subtotals[offer.shop_id] += offer.positionspreis
    for shop_id in shop_ids:
        minimum = shops_by_id[shop_id].mindestbestellwert_chf
        if minimum is not None and subtotals[shop_id] < minimum:
            return None
    shipping: dict[int, Decimal] = {}
    for shop_id in shop_ids:
        shop = shops_by_id[shop_id]
        free_from = shop.gratis_ab_chf
        shipping[shop_id] = (
            Decimal("0.00")
            if free_from is not None and subtotals[shop_id] >= free_from
            else shop.versand_chf
        )
    total = sum(subtotals.values(), Decimal("0.00")) + sum(
        shipping.values(), Decimal("0.00")
    )
    max_days = max(
        offer.lieferzeit_tage or shops_by_id[offer.shop_id].lieferzeit_default_tage
        for offer in chosen.values()
    )
    score = total + Decimal(str(tempo)) * TEMPO_COST_PER_DAY_CHF * max_days
    return OrderVariant(
        shop_ids=shop_ids,
        assignments={line_id: offer.id for line_id, offer in chosen.items()},
        subtotals=subtotals,
        shipping=shipping,
        total_chf=total,
        max_liefertage=max_days,
        score=score,
        contains_estimates=any(offer.lieferzeit_tage is None for offer in chosen.values()),
        missing_line_ids=missing_line_ids,
    )


def _complete_variants(
    offers_by_line: dict[int, list[Offer]],
    shops_by_id: dict[int, ShopProfile],
    required_line_ids: list[int],
    tempo: float,
) -> list[OrderVariant]:
    ordered_lines = sorted(set(required_line_ids))
    if not ordered_lines or any(not offers_by_line.get(line_id) for line_id in ordered_lines):
        return []
    variants: list[OrderVariant] = []
    for assignment in product(*(offers_by_line[line_id] for line_id in ordered_lines)):
        variant = _build_variant(dict(zip(ordered_lines, assignment)), shops_by_id, tempo)
        if variant is not None:
            variants.append(variant)
    return variants


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
    shops_by_id, offers_by_line = _validated_inputs(offers, shops)
    variants = _complete_variants(offers_by_line, shops_by_id, list(offers_by_line), tempo)
    best_by_shop_set: dict[tuple[int, ...], OrderVariant] = {}
    for variant in variants:
        key = variant.shop_ids
        rank = (variant.score, variant.total_chf, variant.max_liefertage, tuple(variant.assignments.items()))
        current = best_by_shop_set.get(key)
        if current is None or rank < (
            current.score,
            current.total_chf,
            current.max_liefertage,
            tuple(current.assignments.items()),
        ):
            best_by_shop_set[key] = variant
    result = list(best_by_shop_set.values())
    result.sort(key=lambda item: (item.score, item.total_chf, item.max_liefertage, item.shop_ids))
    return result[:limit]


def plan_scenarios(
    offers: list[Offer],
    shops: list[ShopProfile],
    required_line_ids: list[int],
    pins: dict[int, int] | None = None,
    excludes: set[int] | None = None,
) -> dict[str, OrderVariant]:
    """Return the four named presets, applying optional pin/exclude overrides."""
    shops_by_id, offers_by_line = _validated_inputs(offers, shops)
    filtered = _filter_overrides(offers_by_line, pins, excludes)
    complete = _complete_variants(filtered, shops_by_id, required_line_ids, tempo=0.5)
    scenarios: dict[str, OrderVariant] = {}
    if complete:
        scenarios["cheapest"] = min(
            complete, key=lambda item: (item.total_chf, item.max_liefertage, len(item.shop_ids), item.shop_ids)
        )
        scenarios["fastest"] = min(
            complete, key=lambda item: (item.max_liefertage, item.total_chf, len(item.shop_ids), item.shop_ids)
        )
        scenarios["balanced"] = min(
            complete, key=lambda item: (item.score, item.total_chf, item.max_liefertage, item.shop_ids)
        )

    required = tuple(sorted(set(required_line_ids)))
    one_shop_candidates: list[OrderVariant] = []
    for shop_id in sorted(shops_by_id):
        chosen: dict[int, Offer] = {}
        for line_id in required:
            candidates = [offer for offer in filtered.get(line_id, []) if offer.shop_id == shop_id]
            if candidates:
                chosen[line_id] = min(
                    candidates,
                    key=lambda offer: (
                        offer.positionspreis,
                        offer.lieferzeit_tage or shops_by_id[shop_id].lieferzeit_default_tage,
                        offer.id,
                    ),
                )
        missing = tuple(line_id for line_id in required if line_id not in chosen)
        variant = _build_variant(chosen, shops_by_id, tempo=0, missing_line_ids=missing)
        if variant is not None:
            one_shop_candidates.append(variant)
    if one_shop_candidates:
        scenarios["one_shop"] = min(
            one_shop_candidates,
            key=lambda item: (
                len(item.missing_line_ids),
                item.total_chf,
                item.max_liefertage,
                item.shop_ids,
            ),
        )
    return scenarios
