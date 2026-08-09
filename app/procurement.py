from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlparse

from .optimizer import Offer, ShopProfile, optimize_orders


class ValidationError(ValueError):
    pass


class ProcurementRepository(Protocol):
    def next_job(self) -> dict[str, Any] | None: ...
    def check_line(self, line_id: int) -> dict[str, Any] | None: ...
    def create_shop(self, **values: Any) -> dict[str, Any]: ...
    def get_shop(self, shop_id: int) -> dict[str, Any] | None: ...
    def get_line(self, line_id: int) -> dict[str, Any] | None: ...
    def create_offer(self, **values: Any) -> dict[str, Any]: ...
    def mark_line(self, line_id: int, status: str, kommentar: str | None) -> dict[str, Any]: ...
    def optimization_input(self, job_id: int) -> dict[str, Any]: ...
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

    def record_shop(
        self,
        name: str,
        url: str,
        land: str,
        versand_chf: Any,
        gratis_ab_chf: Any | None,
        mindestbestellwert_chf: Any | None,
        lieferzeit_default_tage: int,
    ) -> dict[str, Any]:
        if land != "CH":
            raise ValidationError("Es sind nur Shops aus der Schweiz (land=CH) erlaubt")
        if not name.strip():
            raise ValidationError("Shop-Name fehlt")
        domain = _hostname(url, "Shop-URL")
        shipping = _decimal(versand_chf, "Versand")
        free_from = None if gratis_ab_chf is None else _decimal(gratis_ab_chf, "Gratisgrenze")
        minimum = (
            None
            if mindestbestellwert_chf is None
            else _decimal(mindestbestellwert_chf, "Mindestbestellwert")
        )
        if not isinstance(lieferzeit_default_tage, int) or lieferzeit_default_tage <= 0:
            raise ValidationError("Standard-Lieferzeit muss eine positive ganze Tageszahl sein")
        return self.repository.create_shop(
            name=name.strip(),
            url=url,
            domain=domain,
            land="CH",
            versand_chf=shipping,
            gratis_ab_chf=free_from,
            mindestbestellwert_chf=minimum,
            lieferzeit_default_tage=lieferzeit_default_tage,
        )

    def record_offer(
        self,
        line_id: int,
        shop_id: int,
        produktname: str,
        produkt_url: str,
        preis_chf: Any,
        lieferzeit_tage: int | None = None,
        lager: str | None = None,
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
        if lieferzeit_tage is not None and (
            not isinstance(lieferzeit_tage, int) or lieferzeit_tage <= 0
        ):
            raise ValidationError("Lieferzeit muss eine positive ganze Tageszahl sein")
        return self.repository.create_offer(
            line_id=line_id,
            shop_id=shop_id,
            produktname=produktname.strip(),
            produkt_url=produkt_url,
            quelle_url=produkt_url,
            preis_chf=price,
            lieferzeit_tage=lieferzeit_tage,
            lager=lager.strip() if lager else None,
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
        return [self._serialize_variant(variant) for variant in optimize_orders(offers, shops, tempo)]

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
    def _optimizer_objects(data: dict[str, Any]) -> tuple[list[Offer], list[ShopProfile]]:
        offers = [
            Offer(
                id=int(row["id"]),
                line_id=int(row["line_id"]),
                shop_id=int(row["shop_id"]),
                preis_chf=Decimal(str(row["preis_chf"])),
                menge=int(row["menge"]),
                lieferzeit_tage=row.get("lieferzeit_tage"),
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
                lieferzeit_default_tage=int(row["lieferzeit_default_tage"]),
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
        }
