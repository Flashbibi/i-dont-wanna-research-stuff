from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from .optimizer import (
    Offer,
    ShopProfile,
    filter_dominated_variants,
    optimize_orders,
    plan_scenarios as build_scenarios,
)


class ValidationError(ValueError):
    pass


class ProcurementRepository(Protocol):
    def next_job(self) -> dict[str, Any] | None: ...
    def check_line(self, line_id: int) -> dict[str, Any] | None: ...
    def create_shop(self, **values: Any) -> dict[str, Any]: ...
    def get_shop(self, shop_id: int) -> dict[str, Any] | None: ...
    def update_shop_profile(self, shop_id: int, **values: Any) -> dict[str, Any]: ...
    def get_line(self, line_id: int) -> dict[str, Any] | None: ...
    def create_offer(self, **values: Any) -> dict[str, Any]: ...
    def mark_line(self, line_id: int, status: str, kommentar: str | None) -> dict[str, Any]: ...
    def optimization_input(self, job_id: int) -> dict[str, Any]: ...
    def save_job_selection(
        self, job_id: int, assignments: dict[str, int]
    ) -> dict[str, Any]: ...
    def create_purchase(
        self,
        job_id: int,
        variant: dict[str, Any],
        ordered_at: datetime,
        promised_days: dict[str, int],
    ) -> dict[str, Any]: ...


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError(f"{field} muss eine Zahl sein") from error
    if positive and result <= 0:
        raise ValidationError(f"{field} muss groesser als 0 sein")
    if not positive and result < 0:
        raise ValidationError(f"{field} darf nicht negativ sein")
    return result


def _hostname(url: str, field: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValidationError(f"{field} muss eine gueltige HTTP(S)-URL ohne Zugangsdaten sein")
    host = parsed.hostname.lower().rstrip(".")
    return host.removeprefix("www.")


def parse_delivery_upper_days(text: str | None) -> int | None:
    """Parse an explicit day count using the conservative range upper bound."""
    if not text or not text.strip():
        return None
    day_word = r"(?:Arbeits|Werk)?tag(?:e|en)?"
    range_match = re.search(
        rf"\b(\d+)\s*(?:-|–|—|bis)\s*(\d+)\s*{day_word}\b",
        text,
        re.IGNORECASE,
    )
    if range_match:
        return max(int(range_match.group(1)), int(range_match.group(2)))
    single_match = re.search(rf"\b(\d+)\s*{day_word}\b", text, re.IGNORECASE)
    return int(single_match.group(1)) if single_match else None


class ProcurementService:
    def __init__(self, repository: ProcurementRepository):
        self.repository = repository

    def next_job(self) -> dict[str, Any] | None:
        return self.repository.next_job()

    def check_line(self, line_id: int) -> dict[str, Any]:
        result = self.repository.check_line(line_id)
        if result is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        return result

    def _validated_shop_profile(
        self,
        *,
        versand_chf: Any,
        gratis_ab_chf: Any | None,
        mindestbestellwert_chf: Any | None,
        lieferzeit_default_tage: int | None,
        profil_quelle_url: str,
        versand_text: str,
    ) -> dict[str, Any]:
        if not profil_quelle_url or not profil_quelle_url.strip():
            raise ValidationError("Profil-Quelle fehlt")
        _hostname(profil_quelle_url, "Profil-Quelle")
        if not versand_text or not versand_text.strip():
            raise ValidationError("Versand-Originaltext fehlt")
        shipping = _decimal(versand_chf, "Versand")
        free_from = None if gratis_ab_chf is None else _decimal(gratis_ab_chf, "Gratisgrenze")
        minimum = (
            None
            if mindestbestellwert_chf is None
            else _decimal(mindestbestellwert_chf, "Mindestbestellwert")
        )
        if lieferzeit_default_tage is not None and (
            not isinstance(lieferzeit_default_tage, int) or lieferzeit_default_tage <= 0
        ):
            raise ValidationError("Standard-Lieferzeit muss leer oder eine positive ganze Tageszahl sein")
        return {
            "versand_chf": shipping,
            "gratis_ab_chf": free_from,
            "mindestbestellwert_chf": minimum,
            "lieferzeit_default_tage": lieferzeit_default_tage,
            "profil_quelle_url": profil_quelle_url.strip(),
            "versand_text": versand_text.strip(),
        }

    def record_shop(
        self,
        name: str,
        url: str,
        land: str,
        versand_chf: Any,
        gratis_ab_chf: Any | None,
        mindestbestellwert_chf: Any | None,
        lieferzeit_default_tage: int | None,
        profil_quelle_url: str,
        versand_text: str,
    ) -> dict[str, Any]:
        if land != "CH":
            raise ValidationError("Es sind nur Shops aus der Schweiz (land=CH) erlaubt")
        if not name.strip():
            raise ValidationError("Shop-Name fehlt")
        domain = _hostname(url, "Shop-URL")
        profile = self._validated_shop_profile(
            versand_chf=versand_chf,
            gratis_ab_chf=gratis_ab_chf,
            mindestbestellwert_chf=mindestbestellwert_chf,
            lieferzeit_default_tage=lieferzeit_default_tage,
            profil_quelle_url=profil_quelle_url,
            versand_text=versand_text,
        )
        return self.repository.create_shop(
            name=name.strip(),
            url=url,
            domain=domain,
            land="CH",
            **profile,
        )

    def record_shop_profile(
        self,
        shop_id: int,
        *,
        versand_chf: Any,
        gratis_ab_chf: Any | None,
        mindestbestellwert_chf: Any | None,
        lieferzeit_default_tage: int | None,
        profil_quelle_url: str,
        versand_text: str,
    ) -> dict[str, Any]:
        if self.repository.get_shop(shop_id) is None:
            raise ValidationError(f"Shop {shop_id} ist unbekannt")
        profile = self._validated_shop_profile(
            versand_chf=versand_chf,
            gratis_ab_chf=gratis_ab_chf,
            mindestbestellwert_chf=mindestbestellwert_chf,
            lieferzeit_default_tage=lieferzeit_default_tage,
            profil_quelle_url=profil_quelle_url,
            versand_text=versand_text,
        )
        return self.repository.update_shop_profile(shop_id, **profile)

    def record_offer(
        self,
        line_id: int,
        shop_id: int,
        produktname: str,
        produkt_url: str,
        preis_chf: Any,
        lieferzeit_text: str | None = None,
        lager_text: str | None = None,
    ) -> dict[str, Any]:
        if self.repository.get_line(line_id) is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        shop = self.repository.get_shop(shop_id)
        if shop is None:
            raise ValidationError(f"Shop {shop_id} ist unbekannt; zuerst record_shop aufrufen")
        if shop["status"] == "gesperrt":
            raise ValidationError(f"Shop {shop_id} ist gesperrt")
        if not produktname.strip():
            raise ValidationError("Produktname fehlt")
        price = _decimal(preis_chf, "Preis", positive=True)
        product_domain = _hostname(produkt_url, "Produkt-URL")
        shop_domain = str(shop["domain"]).lower().removeprefix("www.")
        if product_domain != shop_domain and not product_domain.endswith("." + shop_domain):
            raise ValidationError(
                f"Produkt-URL passt nicht zur Shop-Domain {shop_domain}"
            )
        normalized_delivery_text = lieferzeit_text.strip() if lieferzeit_text else None
        normalized_stock_text = lager_text.strip() if lager_text else None
        delivery_days = parse_delivery_upper_days(normalized_delivery_text)
        if delivery_days is not None and normalized_delivery_text is None:
            raise ValidationError(
                "Lieferzeit darf nicht ohne wörtlichen Originaltext der Produktseite gesetzt sein"
            )
        return self.repository.create_offer(
            line_id=line_id,
            shop_id=shop_id,
            produktname=produktname.strip(),
            produkt_url=produkt_url,
            quelle_url=produkt_url,
            preis_chf=price,
            lieferzeit_tage=delivery_days,
            lieferzeit_text=normalized_delivery_text,
            lager_text=normalized_stock_text,
            lager=normalized_stock_text,
        )

    def mark_line(
        self, line_id: int, status: str, kommentar: str | None = None
    ) -> dict[str, Any]:
        if self.repository.get_line(line_id) is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        allowed = {"bestand", "nichts_gefunden", "erledigt"}
        if status not in allowed:
            raise ValidationError(f"Status muss einer von {sorted(allowed)} sein")
        return self.repository.mark_line(line_id, status, kommentar)

    def plan_order(self, job_id: int, tempo: float) -> list[dict[str, Any]]:
        if not isinstance(tempo, (int, float)) or isinstance(tempo, bool) or not 0 <= tempo <= 1:
            raise ValidationError("tempo muss zwischen 0 und 1 liegen")
        data = self.repository.optimization_input(job_id)
        offers, shops = self._optimizer_objects(data)
        pins, excludes = self._overrides(data)
        offers = self._apply_overrides(offers, pins, excludes)
        required_line_ids = {
            int(value) for value in data.get("required_line_ids", [offer.line_id for offer in offers])
        }
        if required_line_ids - {offer.line_id for offer in offers}:
            return []
        variants = [
            self._serialize_variant(variant)
            for variant in optimize_orders(offers, shops, tempo)
        ]
        return [self._enrich_variant(variant, data) for variant in variants]

    def plan_scenarios(
        self,
        job_id: int,
        pins: Mapping[int | str, int] | None = None,
        excludes: list[int] | set[int] | None = None,
        tempo: float = 0.5,
    ) -> dict[str, Any]:
        if not isinstance(tempo, (int, float)) or isinstance(tempo, bool) or not 0 <= tempo <= 1:
            raise ValidationError("tempo muss zwischen 0 und 1 liegen")
        data = self.repository.optimization_input(job_id)
        offers, shops = self._optimizer_objects(data)
        persisted_pins, persisted_excludes = self._overrides(data)
        effective_pins = {**persisted_pins, **{int(key): int(value) for key, value in (pins or {}).items()}}
        effective_excludes = persisted_excludes | {int(value) for value in (excludes or [])}
        required = [int(value) for value in data.get("required_line_ids", [])]
        try:
            presets = build_scenarios(
                offers,
                shops,
                required_line_ids=required,
                pins=effective_pins,
                excludes=effective_excludes,
            )
            tuned_offers = self._apply_overrides(offers, effective_pins, effective_excludes)
            tuned_variants = optimize_orders(tuned_offers, shops, tempo)
        except ValueError as error:
            raise ValidationError(str(error)) from error

        labels = {
            "cheapest": "Am günstigsten",
            "fastest": "Am schnellsten",
            "one_shop": "Ein Shop",
            "balanced": "Ausgewogen",
        }
        named_presets = [
            (key, presets[key])
            for key in ("cheapest", "fastest", "one_shop", "balanced")
            if key in presets
        ]
        visible_preset_ids = {
            id(variant)
            for variant in filter_dominated_variants(
                [variant for _, variant in named_presets]
            )
        }
        grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for key, preset in named_presets:
            if id(preset) not in visible_preset_ids:
                continue
            variant = self._enrich_variant(self._serialize_variant(preset), data)
            max_lines = [
                line
                for line in variant["lines"]
                if variant["max_liefertage"] is not None
                and line["lieferzeit_tage"] == variant["max_liefertage"]
            ]
            fastest_estimated_max = (
                key == "fastest"
                and bool(max_lines)
                and all(line["lieferzeit_geschaetzt"] for line in max_lines)
            )
            identity = (
                tuple(sorted(variant["assignments"].items())),
                tuple(variant["shop_ids"]),
                variant["total_chf"],
                variant["max_liefertage"],
                tuple(variant["missing_line_ids"]),
            )
            if identity not in grouped:
                variant["key"] = key
                variant["label"] = labels[key]
                variant["keys"] = [key]
                variant["labels"] = [labels[key]]
                variant["fastest_max_exclusively_estimated"] = fastest_estimated_max
                grouped[identity] = variant
            else:
                grouped[identity]["keys"].append(key)
                grouped[identity]["labels"].append(labels[key])
                grouped[identity]["fastest_max_exclusively_estimated"] = (
                    grouped[identity]["fastest_max_exclusively_estimated"]
                    or fastest_estimated_max
                )
        scenarios = list(grouped.values())
        for variant in scenarios:
            variant["same_result_note"] = (
                "Gleiches Bestellergebnis für mehrere Ziele – bei diesem Angebots-Pool erwartbar."
                if len(variant["keys"]) > 1
                else None
            )
        fine_tuned = [
            self._enrich_variant(self._serialize_variant(variant), data)
            for variant in tuned_variants
        ]
        custom = fine_tuned[0] if fine_tuned else None
        custom_verdict = None
        if custom is not None:
            matching = next(
                (
                    scenario
                    for scenario in scenarios
                    if scenario["assignments"] == custom["assignments"]
                ),
                None,
            )
            if matching is not None:
                max_text = (
                    "Lieferzeit unbekannt"
                    if matching["max_liefertage"] is None
                    else f"max. {matching['max_liefertage']} "
                    f"{'Tag' if matching['max_liefertage'] == 1 else 'Tage'}"
                )
                custom_verdict = (
                    "Ändert bei diesem Angebots-Pool nichts: "
                    f"{matching['labels'][0]} bleibt die beste Lösung ({max_text}). "
                    "Teurere oder unsicherere Pläne werden nicht angezeigt."
                )
                custom = None
            else:
                custom["key"] = "custom"
                custom["keys"] = ["custom"]
                custom["label"] = "Eigene Gewichtung"
                custom["labels"] = ["Eigene Gewichtung"]
        choices: dict[str, list[dict[str, Any]]] = {}
        shop_rows = {int(row["id"]): row for row in data.get("shops", [])}
        for row in data.get("offers", []):
            if int(row["id"]) in effective_excludes:
                continue
            line_key = str(int(row["line_id"]))
            product_key = self._product_key(row.get("produktname"))
            existing_keys = {choice["product_key"] for choice in choices.get(line_key, [])}
            if product_key in existing_keys:
                continue
            choices.setdefault(line_key, []).append(
                {
                    "offer_id": int(row["id"]),
                    "product_key": product_key,
                    "produktname": row.get("produktname"),
                    "shop_name": shop_rows[int(row["shop_id"])]["name"],
                    "preis_chf": str(row["preis_chf"]),
                }
            )
        matrix_lines = self._matrix_lines(data, effective_excludes)
        stored_selection = {
            str(key): int(value)
            for key, value in (data.get("selected_assignments") or {}).items()
        }
        selectable = [*scenarios, *([custom] if custom is not None else [])]
        selected = next(
            (
                variant
                for variant in selectable
                if variant["assignments"] == stored_selection
            ),
            scenarios[0] if scenarios else custom,
        )
        return {
            "job_id": job_id,
            "ready": bool(required) and all(
                any(not candidate["excluded"] for candidate in line["candidates"])
                for line in matrix_lines
                if line["required"]
            ),
            "pins": {str(key): value for key, value in effective_pins.items()},
            "excludes": sorted(effective_excludes),
            "choices": choices,
            "lines": matrix_lines,
            "open_lines": [
                line for line in matrix_lines if not line["required"]
            ],
            "selected_assignments": (
                selected["assignments"] if selected is not None else None
            ),
            "selected_key": selected["key"] if selected is not None else None,
            "scenarios": scenarios,
            "custom": custom,
            "custom_verdict": custom_verdict,
            "fine_tuned": fine_tuned,
        }

    def select_plan(
        self,
        job_id: int,
        assignments: Mapping[Any, int],
        *,
        tempo: float = 0.5,
    ) -> dict[str, Any]:
        normalized = {str(int(key)): int(value) for key, value in assignments.items()}
        matrix = self.plan_scenarios(job_id, tempo=tempo)
        valid = [*matrix["scenarios"], *matrix["fine_tuned"][:1]]
        if not any(variant["assignments"] == normalized for variant in valid):
            raise ValidationError("Plan ist nicht mehr gültig")
        return self.repository.save_job_selection(job_id, normalized)

    def plan_delta(
        self,
        job_id: int,
        *,
        line_id: int,
        offer_id: int,
        base_assignments: Mapping[Any, int],
        tempo: float,
    ) -> dict[str, Any]:
        normalized_base = {
            str(int(key)): int(value) for key, value in base_assignments.items()
        }
        baseline_matrix = self.plan_scenarios(job_id, tempo=tempo)
        baseline = next(
            (
                variant
                for variant in baseline_matrix["scenarios"]
                if variant["assignments"] == normalized_base
            ),
            None,
        )
        if baseline is None:
            raise ValidationError("Gewählter Plan ist nicht mehr gültig")
        target_key = baseline["keys"][0]
        hypothetical = self.plan_scenarios(
            job_id,
            pins={line_id: offer_id},
            tempo=tempo,
        )
        target = next(
            (
                variant
                for variant in hypothetical["scenarios"]
                if target_key in variant["keys"]
            ),
            None,
        )
        if target is None:
            raise ValidationError("Kandidat ist in diesem Plan nicht verfügbar")
        return {
            "assignments": target["assignments"],
            "delta_chf": str(
                Decimal(target["total_chf"]) - Decimal(baseline["total_chf"])
            ),
            "total_chf": target["total_chf"],
            "max_liefertage": target["max_liefertage"],
            "contains_unknown_delivery": target["contains_unknown_delivery"],
        }

    @staticmethod
    def _delivery_fields(row: Mapping[str, Any], shop: Mapping[str, Any]) -> dict[str, Any]:
        direct_days = row.get("lieferzeit_tage")
        shop_days = shop.get("lieferzeit_default_tage")
        conditional = bool(
            re.search(
                r"bei Lieferant|CH-Lieferant",
                " ".join(
                    str(value or "")
                    for value in (row.get("lieferzeit_text"), row.get("lager_text"))
                ),
                re.IGNORECASE,
            )
        )
        if direct_days is not None:
            days = int(direct_days)
            chip = f"{days} {'Tag' if days == 1 else 'Tage'}"
            if conditional:
                chip += " · bedingt"
            source = "produktseite"
        elif shop_days is not None:
            days = int(shop_days)
            chip = f"{days} {'Tag' if days == 1 else 'Tage'} · geschätzt"
            source = "shop_standard"
        else:
            days = None
            chip = "Lieferzeit unbekannt"
            source = "unbekannt"
        return {
            "lieferzeit_tage": days,
            "lieferzeit_quelle": source,
            "lieferzeit_chip": chip,
            "lieferzeit_geschaetzt": source == "shop_standard",
            "lieferzeit_bedingt": conditional and source == "produktseite",
        }

    @classmethod
    def _matrix_lines(
        cls, data: dict[str, Any], excludes: set[int]
    ) -> list[dict[str, Any]]:
        shop_rows = {int(row["id"]): row for row in data.get("shops", [])}
        required = {int(value) for value in data.get("required_line_ids", [])}
        offers_by_line: dict[int, list[dict[str, Any]]] = {}
        for row in data.get("offers", []):
            offer_id = int(row["id"])
            shop = shop_rows[int(row["shop_id"])]
            candidate = {
                "offer_id": offer_id,
                "shop_id": int(row["shop_id"]),
                "shop_name": shop["name"],
                "produktname": row.get("produktname"),
                "produkt_url": row.get("produkt_url"),
                "quelle_url": row.get("quelle_url") or row.get("produkt_url"),
                "preis_chf": str(row["preis_chf"]),
                "lieferzeit_text": row.get("lieferzeit_text"),
                "lager_text": row.get("lager_text"),
                "pinned": row.get("override_status") == "pin",
                "excluded": offer_id in excludes,
                **cls._delivery_fields(row, shop),
            }
            offers_by_line.setdefault(int(row["line_id"]), []).append(candidate)
        result = []
        for row in data.get("lines", []):
            line_id = int(row["id"])
            candidates = offers_by_line.get(line_id, [])
            available_count = sum(not candidate["excluded"] for candidate in candidates)
            for candidate in candidates:
                candidate["last_candidate"] = (
                    not candidate["excluded"] and available_count <= 1
                )
            result.append(
                {
                    "line_id": line_id,
                    "position": int(row.get("position", 0)),
                    "suchtext": row.get("suchtext"),
                    "menge": int(row.get("menge", 1)),
                    "status": row.get("status", "kandidaten"),
                    "kommentar": row.get("kommentar"),
                    "required": line_id in required,
                    "candidates": candidates,
                }
            )
        return sorted(result, key=lambda line: (line["position"], line["line_id"]))

    def record_purchase(
        self,
        job_id: int,
        variante: dict[str, Any],
        bestellt_am: str,
        zugesagt_liefertage_pro_shop: dict[str, int],
    ) -> dict[str, Any]:
        try:
            ordered_at = datetime.fromisoformat(bestellt_am.replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise ValidationError("bestellt_am muss ein ISO-8601-Zeitpunkt sein") from error
        if ordered_at.tzinfo is None:
            raise ValidationError("bestellt_am braucht eine Zeitzone")
        data = self.repository.optimization_input(job_id)
        offers, shops = self._optimizer_objects(data)
        pins, excludes = self._overrides(data)
        offers = self._apply_overrides(offers, pins, excludes)
        required_line_ids = {
            int(value) for value in data.get("required_line_ids", [offer.line_id for offer in offers])
        }
        if required_line_ids - {offer.line_id for offer in offers}:
            raise ValidationError("Variante ist nicht komplett; mindestens eine Zeile ist unbestaetigt")
        valid_variants = [
            self._serialize_variant(item)
            for item in optimize_orders(offers, shops, tempo=0, limit=max(1, 1000))
        ]
        matches = [
            item
            for item in valid_variants
            if item["shop_ids"] == variante.get("shop_ids")
            and item["assignments"] == variante.get("assignments")
            and item["total_chf"] == variante.get("total_chf")
        ]
        if not matches:
            raise ValidationError("Variante ist unvollstaendig, veraendert oder nicht mehr gueltig")
        shop_keys = {str(shop_id) for shop_id in variante["shop_ids"]}
        if set(zugesagt_liefertage_pro_shop) != shop_keys or any(
            not isinstance(days, int) or days <= 0
            for days in zugesagt_liefertage_pro_shop.values()
        ):
            raise ValidationError("Liefertage muessen fuer jeden Shop positiv angegeben sein")
        return self.repository.create_purchase(
            job_id, variante, ordered_at, zugesagt_liefertage_pro_shop
        )

    @staticmethod
    def _product_key(name: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()

    @staticmethod
    def _overrides(data: dict[str, Any]) -> tuple[dict[int, int], set[int]]:
        pins = {
            int(row["line_id"]): int(row["id"])
            for row in data.get("offers", [])
            if row.get("override_status") == "pin"
        }
        excludes = {
            int(row["id"])
            for row in data.get("offers", [])
            if row.get("override_status") == "exclude"
        }
        return pins, excludes

    @staticmethod
    def _apply_overrides(
        offers: list[Offer], pins: dict[int, int], excludes: set[int]
    ) -> list[Offer]:
        available = [offer for offer in offers if offer.id not in excludes]
        pinned_keys = {
            line_id: next(
                (offer.product_key for offer in offers if offer.id == offer_id and offer.line_id == line_id),
                None,
            )
            for line_id, offer_id in pins.items()
        }
        return [
            offer
            for offer in available
            if offer.line_id not in pins
            or (
                offer.product_key == pinned_keys[offer.line_id]
                if pinned_keys[offer.line_id] is not None
                else offer.id == pins[offer.line_id]
            )
        ]

    @staticmethod
    def _enrich_variant(variant: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        offer_rows = {int(row["id"]): row for row in data.get("offers", [])}
        shop_rows = {int(row["id"]): row for row in data.get("shops", [])}
        line_rows = {int(row["id"]): row for row in data.get("lines", [])}
        product_keys: dict[int, set[str]] = {}
        for row in data.get("offers", []):
            key = re.sub(r"[^a-z0-9]+", " ", str(row.get("produktname", "")).lower()).strip()
            product_keys.setdefault(int(row["line_id"]), set()).add(key)

        assignment_shop_counts: dict[int, int] = {}
        for offer_id in variant["assignments"].values():
            shop_id = int(offer_rows[offer_id]["shop_id"])
            assignment_shop_counts[shop_id] = assignment_shop_counts.get(shop_id, 0) + 1
        variant["shops"] = [
            {
                "id": shop_id,
                "name": shop_rows[shop_id]["name"],
                "url": shop_rows[shop_id].get("url"),
                "subtotal_chf": variant["subtotals"][str(shop_id)],
                "versand_chf": variant["shipping"][str(shop_id)],
                "gratis_ab_chf": (
                    None
                    if shop_rows[shop_id].get("gratis_ab_chf") is None
                    else str(shop_rows[shop_id]["gratis_ab_chf"])
                ),
                "artikelanzahl": assignment_shop_counts.get(shop_id, 0),
                "versand_gratis": Decimal(variant["shipping"][str(shop_id)]) == 0,
            }
            for shop_id in variant["shop_ids"]
        ]
        lines = []
        for line_id_text, offer_id in variant["assignments"].items():
            line_id = int(line_id_text)
            row = offer_rows[offer_id]
            shop = shop_rows[int(row["shop_id"])]
            delivery = ProcurementService._delivery_fields(row, shop)
            effective_days = delivery["lieferzeit_tage"]
            assumption = len(product_keys.get(line_id, set())) > 1
            lines.append(
                {
                    "line_id": line_id,
                    "position": int(row.get("position", line_rows.get(line_id, {}).get("position", 0))),
                    "offer_id": offer_id,
                    "suchtext": row.get("suchtext") or line_rows.get(line_id, {}).get("suchtext"),
                    "menge": int(row["menge"]),
                    "shop_id": int(row["shop_id"]),
                    "produktname": row.get("produktname"),
                    "produkt_url": row.get("produkt_url"),
                    "quelle_url": row.get("quelle_url") or row.get("produkt_url"),
                    "einzelpreis_chf": str(row["preis_chf"]),
                    "lieferzeit_tage": (
                        None if effective_days is None else int(effective_days)
                    ),
                    "lieferzeit_text": row.get("lieferzeit_text"),
                    "lager_text": row.get("lager_text"),
                    "lieferzeit_quelle": delivery["lieferzeit_quelle"],
                    "lieferzeit_chip": delivery["lieferzeit_chip"],
                    "lieferzeit_geschaetzt": delivery["lieferzeit_geschaetzt"],
                    "lieferzeit_bedingt": delivery["lieferzeit_bedingt"],
                    "assumption": assumption,
                    "assumption_text": f"Annahme: {row.get('produktname')}" if assumption else None,
                    "pinned": row.get("override_status") == "pin",
                }
            )
        variant["lines"] = sorted(lines, key=lambda row: (row["position"], row["line_id"]))
        variant["missing_lines"] = [
            {
                "line_id": line_id,
                "position": line_rows.get(line_id, {}).get("position"),
                "suchtext": line_rows.get(line_id, {}).get("suchtext"),
            }
            for line_id in variant.get("missing_line_ids", [])
        ]
        variant["complete"] = not variant["missing_lines"]
        variant["incomplete"] = not variant["complete"]
        variant["shop_count"] = len(variant["shop_ids"])
        return variant

    @staticmethod
    def _optimizer_objects(data: dict[str, Any]) -> tuple[list[Offer], list[ShopProfile]]:
        offers = [
            Offer(
                id=int(row["id"]),
                line_id=int(row["line_id"]),
                shop_id=int(row["shop_id"]),
                preis_chf=Decimal(str(row["preis_chf"])),
                menge=int(row["menge"]),
                lieferzeit_tage=row.get("lieferzeit_tage"),
                product_key=ProcurementService._product_key(row.get("produktname")),
            )
            for row in data.get("offers", [])
        ]
        shops = [
            ShopProfile(
                id=int(row["id"]),
                name=row["name"],
                versand_chf=Decimal(str(row["versand_chf"])),
                gratis_ab_chf=(
                    None
                    if row.get("gratis_ab_chf") is None
                    else Decimal(str(row["gratis_ab_chf"]))
                ),
                mindestbestellwert_chf=(
                    None
                    if row.get("mindestbestellwert_chf") is None
                    else Decimal(str(row["mindestbestellwert_chf"]))
                ),
                lieferzeit_default_tage=(
                    None
                    if row.get("lieferzeit_default_tage") is None
                    else int(row["lieferzeit_default_tage"])
                ),
            )
            for row in data.get("shops", [])
        ]
        return offers, shops

    @staticmethod
    def _serialize_variant(variant: Any) -> dict[str, Any]:
        return {
            "shop_ids": list(variant.shop_ids),
            "assignments": {str(key): value for key, value in variant.assignments.items()},
            "subtotals": {str(key): str(value) for key, value in variant.subtotals.items()},
            "shipping": {str(key): str(value) for key, value in variant.shipping.items()},
            "total_chf": str(variant.total_chf),
            "max_liefertage": variant.max_liefertage,
            "score": str(variant.score),
            "contains_estimates": variant.contains_estimates,
            "contains_unknown_delivery": variant.contains_unknown_delivery,
            "missing_line_ids": list(variant.missing_line_ids),
        }
