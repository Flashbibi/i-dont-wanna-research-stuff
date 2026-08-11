"""Reine, deterministische Bestelloptimierung ohne Datenbank- oder Netzwerkzugriff."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import product


TEMPO_COST_PER_DAY_CHF = Decimal("15.00")
UNKNOWN_DELIVERY_SCORE_DAYS = 10_000
UNKNOWN_DELIVERY_RANK = 10_000


def _delivery_rank(days: int | None) -> tuple[int, int]:
    """Known delivery always ranks before unknown; unknown is never treated as zero."""
    if days is None:
        return (1, UNKNOWN_DELIVERY_RANK)
    return (0, days)


@dataclass(frozen=True)
class ShopProfile:
    id: int
    name: str
    versand_chf: Decimal
    gratis_ab_chf: Decimal | None
    mindestbestellwert_chf: Decimal | None
    lieferzeit_default_tage: int | None
    #: Lieferziel des Shops. Die Heimadresse kostet nichts extra; ein fremdes
    #: Ziel kostet Linus eine Abholfahrt und Wartezeit.
    lieferziel_id: int | None = None
    lieferziel_name: str | None = None
    aufschlag_chf: Decimal = Decimal("0.00")
    zuschlag_tage: int = 0
    ist_heimat: bool = True


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
    max_liefertage: int | None
    score: Decimal
    contains_estimates: bool = False
    contains_unknown_delivery: bool = False
    missing_line_ids: tuple[int, ...] = ()
    #: (lieferziel_id, name, betrag) je beteiligtem Nicht-Heim-Ziel, einmal pro
    #: Ziel und Plan - eine Abholfahrt, egal wie viele Shops dort liegen.
    aufschlaege: tuple[tuple[int, str, Decimal], ...] = ()

    @property
    def aufschlag_chf(self) -> Decimal:
        return sum((betrag for _, _, betrag in self.aufschlaege), Decimal("0.00"))


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
    # Abhol-Aufschlag: einmal pro beteiligtem Nicht-Heim-Ziel, nicht pro Shop.
    # Zwei deutsche Shops im selben Plan sind eine Fahrt, nicht zwei.
    aufschlaege_nach_ziel: dict[int, tuple[str, Decimal]] = {}
    for shop_id in shop_ids:
        shop = shops_by_id[shop_id]
        if shop.ist_heimat or shop.lieferziel_id is None:
            continue
        aufschlaege_nach_ziel[shop.lieferziel_id] = (
            shop.lieferziel_name or f"Ziel {shop.lieferziel_id}",
            shop.aufschlag_chf,
        )
    aufschlaege = tuple(
        (ziel_id, name, betrag)
        for ziel_id, (name, betrag) in sorted(aufschlaege_nach_ziel.items())
    )

    total = (
        sum(subtotals.values(), Decimal("0.00"))
        + sum(shipping.values(), Decimal("0.00"))
        + sum((betrag for _, _, betrag in aufschlaege), Decimal("0.00"))
    )
    # Wartezeit bis zur Abholung liegt oben auf der Lieferzeit jedes Shops
    # dieses Ziels.
    effective_days = [
        None
        if (
            offer.lieferzeit_tage
            if offer.lieferzeit_tage is not None
            else shops_by_id[offer.shop_id].lieferzeit_default_tage
        )
        is None
        else (
            (
                offer.lieferzeit_tage
                if offer.lieferzeit_tage is not None
                else shops_by_id[offer.shop_id].lieferzeit_default_tage
            )
            + shops_by_id[offer.shop_id].zuschlag_tage
        )
        for offer in chosen.values()
    ]
    known_days = [value for value in effective_days if value is not None]
    max_days = None if len(known_days) != len(effective_days) else max(known_days)
    score_days = UNKNOWN_DELIVERY_SCORE_DAYS if max_days is None else max_days
    score = total + Decimal(str(tempo)) * TEMPO_COST_PER_DAY_CHF * score_days
    return OrderVariant(
        shop_ids=shop_ids,
        assignments={line_id: offer.id for line_id, offer in chosen.items()},
        subtotals=subtotals,
        shipping=shipping,
        total_chf=total,
        max_liefertage=max_days,
        score=score,
        contains_estimates=any(
            offer.lieferzeit_tage is None
            and shops_by_id[offer.shop_id].lieferzeit_default_tage is not None
            for offer in chosen.values()
        ),
        contains_unknown_delivery=max_days is None,
        missing_line_ids=missing_line_ids,
        aufschlaege=aufschlaege,
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
        rank = (
            variant.score,
            variant.total_chf,
            _delivery_rank(variant.max_liefertage),
            tuple(variant.assignments.items()),
        )
        current = best_by_shop_set.get(key)
        if current is None or rank < (
            current.score,
            current.total_chf,
            _delivery_rank(current.max_liefertage),
            tuple(current.assignments.items()),
        ):
            best_by_shop_set[key] = variant
    result = list(best_by_shop_set.values())
    result.sort(
        key=lambda item: (
            item.score,
            item.total_chf,
            _delivery_rank(item.max_liefertage),
            item.shop_ids,
        )
    )
    return result[:limit]


def filter_dominated_variants(variants: list[OrderVariant]) -> list[OrderVariant]:
    """Hide complete plans that are no cheaper and no faster than another plan.

    Partial one-shop compromises are intentionally incomparable with complete plans.
    """
    result: list[OrderVariant] = []
    for candidate in variants:
        if candidate.missing_line_ids:
            result.append(candidate)
            continue
        dominated = any(
            other is not candidate
            and not other.missing_line_ids
            and other.total_chf <= candidate.total_chf
            and _delivery_rank(other.max_liefertage)
            <= _delivery_rank(candidate.max_liefertage)
            and (
                other.total_chf < candidate.total_chf
                or _delivery_rank(other.max_liefertage)
                < _delivery_rank(candidate.max_liefertage)
            )
            for other in variants
        )
        if not dominated:
            result.append(candidate)
    return result


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
            complete,
            key=lambda item: (
                item.total_chf,
                _delivery_rank(item.max_liefertage),
                len(item.shop_ids),
                item.shop_ids,
            ),
        )
        scenarios["fastest"] = min(
            complete,
            key=lambda item: (
                _delivery_rank(item.max_liefertage),
                item.total_chf,
                len(item.shop_ids),
                item.shop_ids,
            ),
        )
        scenarios["balanced"] = min(
            complete,
            key=lambda item: (
                item.score,
                item.total_chf,
                _delivery_rank(item.max_liefertage),
                item.shop_ids,
            ),
        )

    # «Nur Schweiz»: identische Rechnung, eingeschränkt auf Shops mit
    # Heim-Lieferziel. Deckt sich das Ergebnis mit dem Gesamtoptimum - solange
    # es keine Auslandsangebote gibt, ist das der Normalfall - verschmelzen die
    # Labels weiter oben über die gemeinsame Identität.
    heimat_shops = {
        shop_id for shop_id, shop in shops_by_id.items() if shop.ist_heimat
    }
    nur_heimat = {
        line_id: [offer for offer in offers if offer.shop_id in heimat_shops]
        for line_id, offers in filtered.items()
    }
    required = tuple(sorted(set(required_line_ids)))
    bester_heimat = lambda kandidaten: min(  # noqa: E731
        kandidaten,
        key=lambda item: (
            item.score,
            item.total_chf,
            _delivery_rank(item.max_liefertage),
            item.shop_ids,
        ),
    )
    heimat_komplett = _complete_variants(
        nur_heimat, shops_by_id, required_line_ids, tempo=0.5
    )
    if heimat_komplett:
        scenarios["only_ch"] = bester_heimat(heimat_komplett)
    else:
        # Deckt der Heimmarkt nicht jede Zeile ab, verschwindet das Preset
        # nicht - es erscheint unvollständig wie «Ein Shop». Die Liste der
        # offenen Zeilen ist genau die Auskunft, welche Positionen ins Ausland
        # zwingen; die wäre weg, wenn der Plan stillschweigend entfiele.
        abgedeckt = [line_id for line_id in required if nur_heimat.get(line_id)]
        fehlend = tuple(line_id for line_id in required if line_id not in abgedeckt)
        teilweise = _complete_variants(nur_heimat, shops_by_id, abgedeckt, tempo=0.5)
        if teilweise:
            scenarios["only_ch"] = replace(
                bester_heimat(teilweise), missing_line_ids=fehlend
            )
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
                        _delivery_rank(
                            offer.lieferzeit_tage
                            if offer.lieferzeit_tage is not None
                            else shops_by_id[shop_id].lieferzeit_default_tage
                        ),
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
                _delivery_rank(item.max_liefertage),
                item.shop_ids,
            ),
        )
    return scenarios
