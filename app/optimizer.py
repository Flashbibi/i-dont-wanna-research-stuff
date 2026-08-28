"""Reine, deterministische Bestelloptimierung ohne Datenbank- oder Netzwerkzugriff."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import combinations, product

TEMPO_COST_PER_DAY_CHF = Decimal("15.00")
UNKNOWN_DELIVERY_SCORE_DAYS = 10_000
UNKNOWN_DELIVERY_RANK = 10_000


def _delivery_rank(days: int | None) -> tuple[int, int]:
    """Bekannte Lieferzeit steht immer vor unbekannter; unbekannt gilt nie als null."""
    if days is None:
        return (1, UNKNOWN_DELIVERY_RANK)
    return (0, days)


@dataclass(frozen=True)
class ShopProfile:
    id: int
    name: str
    versand_chf: Decimal | None
    gratis_ab_chf: Decimal | None
    mindestbestellwert_chf: Decimal | None
    lieferzeit_default_tage: int | None
    # Ein fremdes Lieferziel kostet Abholfahrt und Wartezeit, die Heimadresse nichts.
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
    shipping: dict[int, Decimal | None]
    total_chf: Decimal
    max_liefertage: int | None
    score: Decimal
    contains_estimates: bool = False
    contains_unknown_delivery: bool = False
    contains_unknown_shipping: bool = False
    missing_line_ids: tuple[int, ...] = ()
    # (lieferziel_id, name, betrag) je beteiligtem Nicht-Heim-Ziel - eine Abholfahrt,
    # egal wie viele Shops dort liegen.
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
            pinned = next(
                (offer for offer in line_offers if offer.id == pins[line_id]), None
            )
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
                raise ValueError(
                    f"Gepinntes Produkt für Zeile {line_id} ist vollständig ausgeschlossen"
                )
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
    subtotals = {shop_id: Decimal("0.00") for shop_id in shop_ids}
    for offer in chosen.values():
        subtotals[offer.shop_id] += offer.positionspreis
    for shop_id in shop_ids:
        minimum = shops_by_id[shop_id].mindestbestellwert_chf
        if minimum is not None and subtotals[shop_id] < minimum:
            return None
    shipping: dict[int, Decimal | None] = {}
    for shop_id in shop_ids:
        shop = shops_by_id[shop_id]
        free_from = shop.gratis_ab_chf
        shipping[shop_id] = (
            Decimal("0.00")
            if free_from is not None and subtotals[shop_id] >= free_from
            else shop.versand_chf
        )
    # Abhol-Aufschlag einmal pro beteiligtem Nicht-Heim-Ziel, nicht pro Shop.
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
        + sum(
            (value for value in shipping.values() if value is not None), Decimal("0.00")
        )
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
    contains_unknown_shipping = any(value is None for value in shipping.values())
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
        contains_unknown_shipping=contains_unknown_shipping,
        missing_line_ids=missing_line_ids,
        aufschlaege=aufschlaege,
    )


def _effektive_tage(offer: Offer, shops_by_id: dict[int, ShopProfile]) -> int | None:
    """Lieferzeit inklusive Wartezeit des Lieferziels; None heisst unbekannt."""
    shop = shops_by_id[offer.shop_id]
    tage = offer.lieferzeit_tage
    if tage is None:
        tage = shop.lieferzeit_default_tage
    return None if tage is None else tage + shop.zuschlag_tage


def _guenstigste_zuordnung(
    ordered_lines: list[int],
    kandidaten: dict[int, list[Offer]],
    shops_by_id: dict[int, ShopProfile],
    obergrenze_tage: int | None,
) -> dict[int, Offer] | None:
    """Pro Zeile das billigste Angebot innerhalb der Lieferzeit-Obergrenze, wobei
    ``obergrenze_tage=None`` auch Angebote mit unbekannter Lieferzeit zulässt."""
    chosen: dict[int, Offer] = {}
    for line_id in ordered_lines:
        pool = kandidaten[line_id]
        if obergrenze_tage is not None:
            pool = [
                offer
                for offer in pool
                if (tage := _effektive_tage(offer, shops_by_id)) is not None
                and tage <= obergrenze_tage
            ]
        if not pool:
            return None
        chosen[line_id] = min(
            pool,
            key=lambda offer: (
                offer.positionspreis,
                _delivery_rank(_effektive_tage(offer, shops_by_id)),
                offer.id,
            ),
        )
    return chosen


def _subtotals(zuordnung: dict[int, Offer]) -> dict[int, Decimal]:
    summen: dict[int, Decimal] = {}
    for offer in zuordnung.values():
        summen[offer.shop_id] = (
            summen.get(offer.shop_id, Decimal("0.00")) + offer.positionspreis
        )
    return summen


def _aufstocken(
    zuordnung: dict[int, Offer],
    kandidaten: dict[int, list[Offer]],
    shops_by_id: dict[int, ShopProfile],
    shop_id: int,
    ziel: Decimal,
    obergrenze_tage: int | None,
) -> dict[int, Offer] | None:
    """Bei einem Shop teurer einkaufen, bis seine Zwischensumme ``ziel`` erreicht."""
    aufgestockt = dict(zuordnung)
    fehlt = ziel - _subtotals(zuordnung).get(shop_id, Decimal("0.00"))
    if fehlt <= 0:
        return aufgestockt

    def erlaubt(offer: Offer) -> bool:
        if obergrenze_tage is None:
            return True
        tage = _effektive_tage(offer, shops_by_id)
        return tage is not None and tage <= obergrenze_tage

    # Zwei Wege, die Zwischensumme zu heben: teurere Variante im Shop oder eine Zeile
    # aus einem anderen Shop.
    schritte: list[tuple[Decimal, Decimal, int, Offer]] = []
    for line_id, aktuell in zuordnung.items():
        for offer in kandidaten[line_id]:
            if offer.shop_id != shop_id or not erlaubt(offer):
                continue
            mehrkosten = offer.positionspreis - aktuell.positionspreis
            beitrag = (
                offer.positionspreis - aktuell.positionspreis
                if aktuell.shop_id == shop_id
                else offer.positionspreis
            )
            if beitrag > 0:
                schritte.append((mehrkosten, beitrag, line_id, offer))
    schritte.sort(key=lambda eintrag: (eintrag[0], -eintrag[1], eintrag[3].id))

    gewonnen = Decimal("0.00")
    benutzt: set[int] = set()
    for _mehrkosten, beitrag, line_id, offer in schritte:
        if gewonnen >= fehlt:
            break
        if line_id in benutzt:
            continue
        benutzt.add(line_id)
        gewonnen += beitrag
        aufgestockt[line_id] = offer
    return aufgestockt if gewonnen >= fehlt else None


def _konzentriert_auf(
    zuordnung: dict[int, Offer],
    kandidaten: dict[int, list[Offer]],
    shops_by_id: dict[int, ShopProfile],
    shop_id: int,
    obergrenze_tage: int | None,
) -> dict[int, Offer] | None:
    """Möglichst viel bei einem Shop bündeln, was einen zweiten Versand spart und die
    Zwischensumme hebt, wo schrittweises Aufstocken nicht reicht."""
    gebuendelt = dict(zuordnung)
    for line_id in zuordnung:
        pool = [
            offer
            for offer in kandidaten[line_id]
            if offer.shop_id == shop_id
            and (
                obergrenze_tage is None
                or (
                    (tage := _effektive_tage(offer, shops_by_id)) is not None
                    and tage <= obergrenze_tage
                )
            )
        ]
        if pool:
            gebuendelt[line_id] = min(
                pool, key=lambda offer: (offer.positionspreis, offer.id)
            )
    return gebuendelt


def _schwellen_varianten(
    zuordnung: dict[int, Offer],
    kandidaten: dict[int, list[Offer]],
    shops_by_id: dict[int, ShopProfile],
    obergrenze_tage: int | None,
) -> list[dict[int, Offer]]:
    """Zusätzliche Kandidaten rund um Mindestbestellwert und Gratisgrenze; die Rückgabe
    ergänzt nur, ausgewählt wird am Ende weiterhin nach Total."""
    varianten: list[dict[int, Offer]] = []

    # 1. Harte Grenze: fehlende Mindestbestellwerte reparieren.
    repariert: dict[int, Offer] | None = dict(zuordnung)
    for shop_id, subtotal in _subtotals(zuordnung).items():
        minimum = shops_by_id[shop_id].mindestbestellwert_chf
        if minimum is None or subtotal >= minimum or repariert is None:
            continue
        repariert = _aufstocken(
            repariert, kandidaten, shops_by_id, shop_id, minimum, obergrenze_tage
        )
    if repariert is not None and repariert != zuordnung:
        varianten.append(repariert)

    # 2. Lohnende Grenze: Gratisversand, ausgehend von beiden Ständen.
    for basis in [
        zuordnung,
        *([repariert] if repariert is not None and repariert != zuordnung else []),
    ]:
        for shop_id, subtotal in _subtotals(basis).items():
            shop = shops_by_id[shop_id]
            grenze = shop.gratis_ab_chf
            if grenze is None or subtotal >= grenze:
                continue
            versand = shop.versand_chf
            # Aufstocken über den Versandpreis hinaus lohnt nie, ausser bei unbekanntem
            # Versand, wo die Gratisgrenze zugleich die Unbekannte im Total beseitigt.
            if versand is not None and grenze - subtotal > versand:
                continue
            gratis = _aufstocken(
                basis, kandidaten, shops_by_id, shop_id, grenze, obergrenze_tage
            )
            if gratis is not None and gratis != basis:
                varianten.append(gratis)
    return varianten


# Ab so vielen Shops wächst 2^n über den Nutzen hinaus; dann nur noch kleine Mengen
# plus die Gesamtmenge.
MAX_SHOPS_FUER_ALLE_TEILMENGEN = 14
KLEINE_TEILMENGE = 4

# Bis hierhin wird vollständig und damit exakt aufgezählt; die Grenze ist eng gewählt,
# weil ein Seitenaufruf drei Aufzählungen auslöst.
KOMBINATIONS_BUDGET = 5_000


def _shop_teilmengen(shop_ids: list[int], max_shops: int) -> list[frozenset[int]]:
    grenze = min(len(shop_ids), max_shops)
    if len(shop_ids) <= MAX_SHOPS_FUER_ALLE_TEILMENGEN:
        groessen = range(1, grenze + 1)
    else:
        # Zu viele Shops für 2^n: kleine Mengen vollständig, dazu die
        # Gesamtmenge, damit das globale Optimum in jedem Fall dabei ist.
        groessen = range(1, min(KLEINE_TEILMENGE, grenze) + 1)
    mengen = [
        frozenset(kombination)
        for groesse in groessen
        for kombination in combinations(shop_ids, groesse)
    ]
    voll = (
        frozenset(shop_ids[:max_shops])
        if len(shop_ids) > max_shops
        else frozenset(shop_ids)
    )
    if voll and voll not in mengen:
        mengen.append(voll)
    return mengen


def _complete_variants(
    offers_by_line: dict[int, list[Offer]],
    shops_by_id: dict[int, ShopProfile],
    required_line_ids: list[int],
    tempo: float,
) -> list[OrderVariant]:
    """Kandidatenpläne über Shop-Mengen statt über Zeilen aufzählen; je Shop-Menge und
    Obergrenze ergibt das billigste Angebot je Zeile die vollständige Pareto-Front."""
    ordered_lines = sorted(set(required_line_ids))
    if not ordered_lines or any(
        not offers_by_line.get(line_id) for line_id in ordered_lines
    ):
        return []

    # Die vollständige Aufzählung ist exakt und wird genommen, solange sie ins
    # Budget passt.
    kombinationen = 1
    for line_id in ordered_lines:
        kombinationen *= len(offers_by_line[line_id])
        if kombinationen > KOMBINATIONS_BUDGET:
            break
    if kombinationen <= KOMBINATIONS_BUDGET:
        return [
            variant
            for belegung in product(
                *(offers_by_line[line_id] for line_id in ordered_lines)
            )
            if (
                variant := _build_variant(
                    dict(zip(ordered_lines, belegung)), shops_by_id, tempo
                )
            )
            is not None
        ]

    beteiligte_shops = sorted(
        {
            offer.shop_id
            for line_id in ordered_lines
            for offer in offers_by_line[line_id]
        }
    )
    if not beteiligte_shops:
        return []

    variants: list[OrderVariant] = []
    gesehen: set[tuple[tuple[int, int], ...]] = set()
    for menge in _shop_teilmengen(beteiligte_shops, len(ordered_lines)):
        kandidaten = {
            line_id: [
                offer for offer in offers_by_line[line_id] if offer.shop_id in menge
            ]
            for line_id in ordered_lines
        }
        if any(not pool for pool in kandidaten.values()):
            continue

        obergrenzen: list[int | None] = sorted(
            {
                tage
                for pool in kandidaten.values()
                for offer in pool
                if (tage := _effektive_tage(offer, shops_by_id)) is not None
            }
        )
        obergrenzen.append(None)

        for grenze in obergrenzen:
            zuordnung = _guenstigste_zuordnung(
                ordered_lines, kandidaten, shops_by_id, grenze
            )
            if zuordnung is None:
                continue
            # Neben der billigsten Zuordnung je Shop eine gebündelte, weil
            # Konzentrieren Versand spart und Gratisgrenzen erreicht.
            basen = [zuordnung]
            for shop_id in sorted(menge):
                gebuendelt = _konzentriert_auf(
                    zuordnung, kandidaten, shops_by_id, shop_id, grenze
                )
                if gebuendelt is not None and gebuendelt != zuordnung:
                    basen.append(gebuendelt)

            erweitert: list[dict[int, Offer]] = []
            for basis in basen:
                erweitert.append(basis)
                erweitert.extend(
                    _schwellen_varianten(basis, kandidaten, shops_by_id, grenze)
                )
            for kandidat in erweitert:
                schluessel = tuple(
                    sorted((line_id, offer.id) for line_id, offer in kandidat.items())
                )
                if schluessel in gesehen:
                    continue
                gesehen.add(schluessel)
                variant = _build_variant(kandidat, shops_by_id, tempo)
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
    variants = _complete_variants(
        offers_by_line, shops_by_id, list(offers_by_line), tempo
    )
    best_by_shop_set: dict[tuple[int, ...], OrderVariant] = {}
    for variant in variants:
        key = variant.shop_ids
        rank = (
            variant.contains_unknown_shipping,
            variant.score,
            variant.total_chf,
            _delivery_rank(variant.max_liefertage),
            tuple(variant.assignments.items()),
        )
        current = best_by_shop_set.get(key)
        if current is None or rank < (
            current.contains_unknown_shipping,
            current.score,
            current.total_chf,
            _delivery_rank(current.max_liefertage),
            tuple(current.assignments.items()),
        ):
            best_by_shop_set[key] = variant
    result = list(best_by_shop_set.values())
    result.sort(
        key=lambda item: (
            item.contains_unknown_shipping,
            item.score,
            item.total_chf,
            _delivery_rank(item.max_liefertage),
            item.shop_ids,
        )
    )
    return result[:limit]


def filter_dominated_variants(variants: list[OrderVariant]) -> list[OrderVariant]:
    """Versteckt dominierte vollständige Pläne; unvollständige Ein-Shop-Kompromisse
    bleiben absichtlich unvergleichbar."""
    result: list[OrderVariant] = []
    for candidate in variants:
        if candidate.missing_line_ids:
            result.append(candidate)
            continue
        dominated = any(
            other is not candidate
            and not other.missing_line_ids
            and other.contains_unknown_shipping <= candidate.contains_unknown_shipping
            and other.total_chf <= candidate.total_chf
            and _delivery_rank(other.max_liefertage)
            <= _delivery_rank(candidate.max_liefertage)
            and (
                other.contains_unknown_shipping < candidate.contains_unknown_shipping
                or other.total_chf < candidate.total_chf
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
    """Liefert die bis zu fünf Presets, jeweils nur wenn es dafür Kandidaten gibt."""
    shops_by_id, offers_by_line = _validated_inputs(offers, shops)
    filtered = _filter_overrides(offers_by_line, pins, excludes)
    complete = _complete_variants(filtered, shops_by_id, required_line_ids, tempo=0.5)
    scenarios: dict[str, OrderVariant] = {}
    if complete:
        scenarios["cheapest"] = min(
            complete,
            key=lambda item: (
                item.contains_unknown_shipping,
                item.total_chf,
                _delivery_rank(item.max_liefertage),
                len(item.shop_ids),
                item.shop_ids,
            ),
        )
        scenarios["fastest"] = min(
            complete,
            key=lambda item: (
                item.contains_unknown_shipping,
                _delivery_rank(item.max_liefertage),
                item.total_chf,
                len(item.shop_ids),
                item.shop_ids,
            ),
        )
        scenarios["balanced"] = min(
            complete,
            key=lambda item: (
                item.contains_unknown_shipping,
                item.score,
                item.total_chf,
                _delivery_rank(item.max_liefertage),
                item.shop_ids,
            ),
        )

    # «Nur Schweiz» rechnet dasselbe nur mit Heim-Shops; deckt sich das Ergebnis
    # mit dem Gesamtoptimum, verschmelzen die Labels über die gemeinsame Identität.
    heimat_shops = {shop_id for shop_id, shop in shops_by_id.items() if shop.ist_heimat}
    nur_heimat = {
        line_id: [offer for offer in offers if offer.shop_id in heimat_shops]
        for line_id, offers in filtered.items()
    }
    required = tuple(sorted(set(required_line_ids)))
    bester_heimat = lambda kandidaten: min(  # noqa: E731
        kandidaten,
        key=lambda item: (
            item.contains_unknown_shipping,
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
        # Das Preset verschwindet nicht, wenn der Heimmarkt eine Zeile nicht abdeckt -
        # die offenen Zeilen sind die Auskunft, welche Positionen ins Ausland zwingen.
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
            candidates = [
                offer for offer in filtered.get(line_id, []) if offer.shop_id == shop_id
            ]
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
                item.contains_unknown_shipping,
                item.total_chf,
                _delivery_rank(item.max_liefertage),
                item.shop_ids,
            ),
        )
    return scenarios
