from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from .adapter import (
    AdapterFehler,
    extrahiere,
    finde_adapter,
    nennt_waehrung,
    parse_preis,
    uebersicht as adapter_uebersicht,
)
from .cart import (
    SUPPORTED_PLATFORMS,
    CartError,
    CartItem,
    build_adapter,
    build_stub_session,
    open_session,
    start_guest_session,
)
from .fetch import FetchFehler, hole_seite
from .jobs import BomInputLine, parse_bom
from .waehrung import (
    HOME_CURRENCY,
    KursError,
    aktueller_kurs,
    nach_chf,
    waehrung_fuer_land,
)
from .optimizer import (
    Offer,
    ShopProfile,
    filter_dominated_variants,
    optimize_orders,
    plan_scenarios as build_scenarios,
)


class ValidationError(ValueError):
    pass


#: Schweizer Wertfreigrenze pro Person und Tag. Nur Anzeige - es wird nie eine
#: Steuer berechnet und nichts davon fliesst in ein Total.
WERTFREIGRENZE_CHF = Decimal("150")

#: Deutscher MwSt-Satz, um aus Bruttopreisen den Nettowert zu naehern.
AUSLAND_MWST = Decimal("1.19")


class ProcurementRepository(Protocol):
    def create_job(self, source_text: str, lines: list[BomInputLine]) -> int: ...
    def delete_unstarted_job(self, job_id: int) -> dict[str, Any]: ...
    def get_job(self, job_id: int) -> dict[str, Any] | None: ...
    def search_history(self, text: str) -> list[dict[str, Any]]: ...
    def get_stock(self) -> list[dict[str, Any]]: ...
    def list_shops(self) -> list[dict[str, Any]]: ...
    def next_job(self) -> dict[str, Any] | None: ...
    def check_line(self, line_id: int) -> dict[str, Any] | None: ...
    def check_stock(self, line_id: int) -> dict[str, Any] | None: ...
    def korrigiere_bestand(
        self, stock_id: int, delta: int, kommentar: str
    ) -> dict[str, Any]: ...
    def create_shop(self, **values: Any) -> dict[str, Any]: ...
    def get_shop(self, shop_id: int) -> dict[str, Any] | None: ...
    def update_shop_profile(self, shop_id: int, **values: Any) -> dict[str, Any]: ...
    def get_line(self, line_id: int) -> dict[str, Any] | None: ...
    def get_offer(self, offer_id: int) -> dict[str, Any] | None: ...
    def create_offer(self, **values: Any) -> dict[str, Any]: ...
    def mark_line(
        self,
        line_id: int,
        status: str,
        kommentar: str | None,
        stock_ids: list[int] | None = None,
    ) -> dict[str, Any]: ...
    def optimization_input(self, job_id: int) -> dict[str, Any]: ...
    def save_job_selection(
        self, job_id: int, assignments: dict[str, int]
    ) -> dict[str, Any]: ...
    def save_shop_platform(
        self, shop_id: int, plattform: str | None, plattform_beleg: str
    ) -> dict[str, Any]: ...
    def save_offer_product_ids(self, produkt_ids: dict[int, str]) -> int: ...
    def save_offer_artikelnummern(self, artikelnummern: dict[int, str]) -> int: ...
    def list_lieferziele(self) -> list[dict[str, Any]]: ...
    def get_lieferziel(self, lieferziel_id: int) -> dict[str, Any] | None: ...
    def lieferziele_fuer_land(self, land: str) -> list[dict[str, Any]]: ...
    def create_lieferziel(self, **values: Any) -> dict[str, Any]: ...
    def update_lieferziel(self, lieferziel_id: int, **values: Any) -> dict[str, Any]: ...
    def get_kurs(self, waehrung: str) -> dict[str, Any] | None: ...
    def save_kurs(
        self, waehrung: str, kurs: Any, geholt_am: Any, quelle_url: str
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
    if not result.is_finite():
        raise ValidationError(f"{field} muss eine endliche Zahl sein")
    if positive and result <= 0:
        raise ValidationError(f"{field} muss groesser als 0 sein")
    if not positive and result < 0:
        raise ValidationError(f"{field} darf nicht negativ sein")
    return result


def _hostname(url: str, field: str) -> str:
    parsed = urlparse(url)
    # username allein reicht nicht: «https://:geheim@shop.ch/» hat einen leeren
    # Benutzernamen und trotzdem ein Passwort, das sonst im Angebot landete.
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValidationError(f"{field} muss eine gueltige HTTP(S)-URL ohne Zugangsdaten sein")
    host = parsed.hostname.lower().rstrip(".")
    return host.removeprefix("www.")


def _adapter_deckt(url: Any) -> bool:
    """Ob ein Adapter diese URL abdeckt - ohne die Seite anzufassen."""
    try:
        finde_adapter(str(url or ""))
    except AdapterFehler:
        return False
    return True


def _gleicher_wert(alt: Any, neu: Any) -> bool:
    """Vergleich über die Grenze Datenbank/Antwort hinweg.

    Preise kommen als ``Decimal``, Texte mal mit und mal ohne Rand-Leerzeichen;
    ein Vergleich der Roh-Objekte meldete Änderungen, die keine sind.
    """
    if alt is None or neu is None:
        return alt is None and neu is None
    if isinstance(alt, Decimal) or isinstance(neu, Decimal):
        try:
            return Decimal(str(alt)) == Decimal(str(neu))
        except InvalidOperation:
            return str(alt) == str(neu)
    if isinstance(alt, str) or isinstance(neu, str):
        return str(alt).strip() == str(neu).strip()
    return alt == neu


def _deckt_domain(host: str, shop_domain: str) -> bool:
    """Produkt-URL und Shop-Domain vergleichen: exakt oder als Subdomain."""
    return host == shop_domain or host.endswith("." + shop_domain)


def _obere_grenze(text: str, einheit: str) -> int | None:
    """Grösste genannte Zahl vor einer Zeiteinheit; Bereiche zählen nach oben."""
    range_match = re.search(
        rf"\b(\d+)\s*(?:-|–|—|bis)\s*(\d+)\s*{einheit}\b",
        text,
        re.IGNORECASE,
    )
    if range_match:
        return max(int(range_match.group(1)), int(range_match.group(2)))
    single_match = re.search(rf"\b(\d+)\s*{einheit}\b", text, re.IGNORECASE)
    return int(single_match.group(1)) if single_match else None


def parse_delivery_upper_days(text: str | None) -> int | None:
    """Genannte Lieferdauer in Tagen lesen, Bereiche konservativ nach oben.

    Tage werden zuerst gesucht und gewinnen: nennt ein Shop beides («2-3 Tage,
    sonst 1-2 Wochen»), ist die Tagesangabe die genauere. Wochen werden mal
    sieben genommen - «1-2 Wochen» sind hier 14 Tage, nicht «unbekannt». Bisher
    fiel genau das durch und der Optimierer verhängte den Unbekannt-Malus,
    obwohl die Auskunft auf der Seite stand. Englische Muster liest diese
    Funktion bewusst nicht.
    """
    if not text or not text.strip():
        return None
    tage = _obere_grenze(text, r"(?:Arbeits|Werk)?tag(?:e|en)?")
    if tage is not None:
        return tage
    wochen = _obere_grenze(text, r"Woche(?:n)?")
    return None if wochen is None else wochen * 7


class ProcurementService:
    MAX_JOB_LINES = 200
    MAX_JOB_LINE_LENGTH = 500

    def __init__(self, repository: ProcurementRepository):
        self.repository = repository

    def create_job(self, source_text: str) -> dict[str, Any]:
        raw_lines = source_text.splitlines()
        if any(len(raw.strip()) > self.MAX_JOB_LINE_LENGTH for raw in raw_lines):
            raise ValidationError("Jede Position darf höchstens 500 Zeichen lang sein")
        lines = parse_bom(source_text)
        if not lines:
            raise ValidationError("Die Liste braucht mindestens eine Position")
        if len(lines) > self.MAX_JOB_LINES:
            raise ValidationError("Ein Job darf höchstens 200 Positionen enthalten")
        job_id = self.repository.create_job(source_text, lines)
        return {
            "job_id": job_id,
            "lines": [
                {"position": line.position, "text": line.suchtext, "menge": line.menge}
                for line in lines
            ],
        }

    def create_job_from_lines(self, lines: list[str]) -> dict[str, Any]:
        if not lines:
            raise ValidationError("Die Liste braucht mindestens eine Position")
        if any(not isinstance(line, str) or not line.strip() for line in lines):
            raise ValidationError("Leere Zeilen sind nicht erlaubt")
        if len(lines) > self.MAX_JOB_LINES:
            raise ValidationError("Ein Job darf höchstens 200 Positionen enthalten")
        return self.create_job("\n".join(lines))

    def delete_job(self, job_id: int, confirm_job_id: int) -> dict[str, Any]:
        if job_id != confirm_job_id:
            raise ValidationError("Bestätigung stimmt nicht mit der Job-ID überein")
        return self.repository.delete_unstarted_job(job_id)

    def get_job(self, job_id: int) -> dict[str, Any]:
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValidationError(f"Job {job_id} ist unbekannt")
        matrix = self.plan_scenarios(job_id)
        candidate_counts = {
            int(line["line_id"]): len(line.get("candidates", []))
            for line in matrix.get("lines", [])
        }
        result = {key: value for key, value in job.items() if key != "lines"}
        result["lines"] = [
            {**line, "candidate_count": candidate_counts.get(int(line["id"]), 0)}
            for line in job.get("lines", [])
        ]
        result["scenarios_available"] = bool(matrix.get("ready")) and bool(
            matrix.get("scenarios")
        )
        return result

    def search_history(self, text: str) -> list[dict[str, Any]]:
        query = text.strip()
        if not query:
            raise ValidationError("Suchtext fehlt")
        if len(query) > 200:
            raise ValidationError("Suchtext darf höchstens 200 Zeichen lang sein")
        return self.repository.search_history(query)

    def get_stock(self) -> list[dict[str, Any]]:
        return self.repository.get_stock()

    def get_shops(self) -> list[dict[str, Any]]:
        shops = [
            {"shop_id": shop["id"], **{key: value for key, value in shop.items() if key != "id"}}
            for shop in self.repository.list_shops()
        ]
        return sorted(
            shops,
            key=lambda shop: (str(shop["name"]).casefold(), int(shop["shop_id"])),
        )

    def next_job(self) -> dict[str, Any] | None:
        return self.repository.next_job()

    def check_line(self, line_id: int) -> dict[str, Any]:
        result = self.repository.check_line(line_id)
        if result is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        return result

    def check_stock(self, line_id: int) -> dict[str, Any]:
        result = self.repository.check_stock(line_id)
        if result is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        return result

    def korrigiere_bestand(
        self, stock_id: int, delta: int, kommentar: str
    ) -> dict[str, Any]:
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValidationError("delta muss ganzzahlig sein")
        if delta == 0:
            raise ValidationError("delta muss ungleich 0 sein")
        normalized_comment = (kommentar or "").strip()
        if not normalized_comment:
            raise ValidationError("Kommentar ist erforderlich")
        try:
            return self.repository.korrigiere_bestand(stock_id, delta, normalized_comment)
        except ValueError as error:
            raise ValidationError(str(error)) from error

    def _validated_shop_profile(
        self,
        *,
        versand_chf: Any,
        gratis_ab_chf: Any | None,
        mindestbestellwert_chf: Any | None,
        lieferzeit_default_tage: int | None,
        profil_quelle_url: str,
        versand_text: str,
        waehrung: str = HOME_CURRENCY,
    ) -> dict[str, Any]:
        if not profil_quelle_url or not profil_quelle_url.strip():
            raise ValidationError("Profil-Quelle fehlt")
        _hostname(profil_quelle_url, "Profil-Quelle")
        if not versand_text or not versand_text.strip():
            raise ValidationError("Versand-Originaltext fehlt")
        shipping = None if versand_chf is None else _decimal(versand_chf, "Versand")
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
        code = (waehrung or HOME_CURRENCY).strip().upper()
        originals = {
            "versand": shipping,
            "gratis_ab": free_from,
            "mindestbestellwert": minimum,
        }
        converted: dict[str, Decimal | None] = {}
        evidence: dict[str, Any] | None = None
        for key, value in originals.items():
            if value is None:
                converted[key] = None
                continue
            result = self._umrechnung(code, value)
            converted[key] = result["preis_chf"]
            evidence = result["spalten"]
        return {
            "versand_chf": converted["versand"],
            "gratis_ab_chf": converted["gratis_ab"],
            "mindestbestellwert_chf": converted["mindestbestellwert"],
            "versand_original": shipping,
            "gratis_ab_original": free_from,
            "mindestbestellwert_original": minimum,
            "versand_waehrung": code,
            "versand_kurs": None if evidence is None else evidence["kurs"],
            "versand_kurs_am": None if evidence is None else evidence["kurs_am"],
            "versand_kurs_quelle": None if evidence is None else evidence["kurs_quelle"],
            "lieferzeit_default_tage": lieferzeit_default_tage,
            "profil_quelle_url": profil_quelle_url.strip(),
            "versand_text": versand_text.strip(),
        }

    # -- Lieferziele ---------------------------------------------------------

    def list_lieferziele(self) -> list[dict[str, Any]]:
        return [
            {**dict(ziel), "ist_heimat": ziel["land"] == "CH"}
            for ziel in self.repository.list_lieferziele()
        ]

    def record_lieferziel(
        self,
        name: str,
        adresse: str,
        land: str,
        *,
        waehrung: str | None = None,
        aufschlag_chf: Any = 0,
        zuschlag_tage: int = 0,
    ) -> dict[str, Any]:
        """Eine Lieferadresse anlegen.

        Semantik, bewusst eng: eine Adresse eröffnet den **Heimmarkt ihres
        Landes**. Eine deutsche Adresse heisst innerdeutscher Versand dorthin -
        nichts weiter. Kein Cross-Border, keine Shop-mal-Adresse-Matrix; was ein
        Shop liefert, beantwortet weiterhin sein belegtes Versandprofil.
        """
        if not name.strip():
            raise ValidationError("Name der Lieferadresse fehlt")
        if not adresse.strip():
            raise ValidationError("Adresse fehlt")
        code = (land or "").strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise ValidationError("Land muss ein zweibuchstabiger Ländercode sein")
        gewaehlt = (waehrung or "").strip().upper() or waehrung_fuer_land(code)
        if not gewaehlt:
            raise ValidationError(
                f"Für Land {code} ist keine Währung hinterlegt; bitte explizit angeben"
            )
        aufschlag = _decimal(aufschlag_chf, "Aufschlag")
        if not isinstance(zuschlag_tage, int) or isinstance(zuschlag_tage, bool) or zuschlag_tage < 0:
            raise ValidationError("Zuschlag-Tage müssen eine Zahl ab 0 sein")
        return self.repository.create_lieferziel(
            name=name.strip(),
            adresse=adresse.strip(),
            land=code,
            waehrung=gewaehlt,
            aufschlag_chf=aufschlag,
            zuschlag_tage=zuschlag_tage,
        )

    def update_lieferziel(
        self,
        lieferziel_id: int,
        *,
        adresse: str,
        waehrung: str,
        aufschlag_chf: Any,
        zuschlag_tage: int,
    ) -> dict[str, Any]:
        if self.repository.get_lieferziel(lieferziel_id) is None:
            raise ValidationError(f"Lieferadresse {lieferziel_id} ist unbekannt")
        if not adresse.strip():
            raise ValidationError("Adresse fehlt")
        if not isinstance(zuschlag_tage, int) or isinstance(zuschlag_tage, bool) or zuschlag_tage < 0:
            raise ValidationError("Zuschlag-Tage müssen eine Zahl ab 0 sein")
        return self.repository.update_lieferziel(
            lieferziel_id,
            adresse=adresse.strip(),
            waehrung=(waehrung or "").strip().upper() or HOME_CURRENCY,
            aufschlag_chf=_decimal(aufschlag_chf, "Aufschlag"),
            zuschlag_tage=zuschlag_tage,
        )

    def _ziel_fuer_land(self, land: str, lieferziel_id: int | None = None) -> dict[str, Any]:
        """Lieferziel bestimmen; Shopland und Ziel dürfen verschieden sein."""
        code = (land or "").strip().upper()
        if lieferziel_id is not None:
            ziel = self.repository.get_lieferziel(lieferziel_id)
            if ziel is None:
                raise ValidationError(f"Lieferadresse {lieferziel_id} ist unbekannt")
            return ziel
        kandidaten = self.repository.lieferziele_fuer_land(code)
        if not kandidaten:
            raise ValidationError(
                f"Keine Lieferadresse für Land {code} konfiguriert; "
                "zuerst eine Lieferadresse anlegen"
            )
        if len(kandidaten) > 1:
            namen = ", ".join(f"«{ziel['name']}»" for ziel in kandidaten)
            raise ValidationError(
                f"Mehrere Lieferadressen in {code} ({namen}); bitte lieferziel_id angeben"
            )
        return kandidaten[0]

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
        lieferziel_id: int | None = None,
        waehrung: str = HOME_CURRENCY,
    ) -> dict[str, Any]:
        code = (land or "").strip().upper()
        if len(code) != 2 or not code.isalpha():
            raise ValidationError("Shop-Land muss ein zweibuchstabiger Ländercode sein")
        ziel = self._ziel_fuer_land(code, lieferziel_id)
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
            waehrung=waehrung,
        )
        return self.repository.create_shop(
            name=name.strip(),
            url=url,
            domain=domain,
            land=code,
            lieferziel_id=int(ziel["id"]),
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
        waehrung: str = HOME_CURRENCY,
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
            waehrung=waehrung,
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
        artikelnummer: str | None = None,
        waehrung: str = HOME_CURRENCY,
        provenienz_text: str | None = None,
        erfasst_via: str | None = None,
    ) -> dict[str, Any]:
        """Ein Angebot erfassen - der einzige Schreibpfad für Angebote.

        ``erfasst_via`` wird nur durchgereicht und ausschliesslich von
        :meth:`fetch_offer` gesetzt. Das MCP-Tool ``record_offer`` reicht den
        Parameter nicht durch: die KI kann sich nicht als Adapter ausgeben.
        """
        if self.repository.get_line(line_id) is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        shop = self.repository.get_shop(shop_id)
        if shop is None:
            raise ValidationError(f"Shop {shop_id} ist unbekannt; zuerst record_shop aufrufen")
        if shop["status"] == "gesperrt":
            raise ValidationError(f"Shop {shop_id} ist gesperrt")
        if not produktname.strip():
            raise ValidationError("Produktname fehlt")
        # Bei Fremdwährung ist der übergebene Betrag der Originalpreis; den
        # CHF-Wert rechnet der Server selbst aus, mit belegtem Tageskurs.
        original = _decimal(preis_chf, "Preis", positive=True)
        umrechnung = self._umrechnung(waehrung, original)
        price = umrechnung["preis_chf"]
        product_domain = _hostname(produkt_url, "Produkt-URL")
        shop_domain = str(shop["domain"]).lower().removeprefix("www.")
        if not _deckt_domain(product_domain, shop_domain):
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
            artikelnummer=(artikelnummer or "").strip() or None,
            provenienz_text=(provenienz_text or "").strip() or None,
            erfasst_via=(erfasst_via or "").strip() or None,
            **umrechnung["spalten"],
        )

    def fetch_offer(self, line_id: int, produkt_url: str) -> dict[str, Any]:
        """Produktseite deterministisch lesen und das Angebot daraus erfassen.

        Der Unterschied zu :meth:`record_offer` ist, wer liest: hier holt die
        Engine die Seite selbst und wendet die Selektoren des Adapters an.
        ``lieferzeit_text`` und ``lager_text`` sind danach wörtlicher
        Seitentext statt einer Behauptung.

        Geschrieben wird trotzdem ausschliesslich über ``record_offer`` - mit
        Originalbetrag und Shopwährung. Damit laufen dieselben Validierungen,
        dieselbe Kursumrechnung und derselbe Lieferzeit-Parser wie beim
        manuellen Weg; einen zweiten Schreibpfad gibt es nicht.
        """
        if self.repository.get_line(line_id) is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        domain = _hostname(produkt_url, "Produkt-URL")
        shop = self._shop_fuer_domain(domain)
        if shop["status"] == "gesperrt":
            raise ValidationError(f"Shop {shop['id']} ist gesperrt")
        try:
            adapter = finde_adapter(produkt_url)
        except AdapterFehler as error:
            raise ValidationError(str(error)) from error

        try:
            seite = hole_seite(produkt_url, min_delay_s=adapter.min_delay_s)
        except FetchFehler as error:
            raise ValidationError(str(error)) from error

        shop_domain = str(shop["domain"]).lower().removeprefix("www.")
        if not _deckt_domain(_hostname(seite.final_url, "Produkt-URL"), shop_domain):
            raise ValidationError(
                f"Weiterleitung verlässt {shop_domain}: {seite.final_url}"
            )
        # Die Shopwährung steht am Shop, nicht auf der Seite: der Preistext darf
        # sie bestätigen, aber nicht bestimmen.
        waehrung = str(shop.get("versand_waehrung") or "").strip().upper()
        if not waehrung:
            raise ValidationError(
                f"Für Shop {shop['id']} ist keine Währung hinterlegt; "
                "zuerst das Shopprofil mit Währung erfassen"
            )
        try:
            felder = extrahiere(adapter, seite.text)
            preistext = felder["preis"] or ""
            self._pruefe_shopwaehrung(shop, waehrung, preistext)
            preis = parse_preis(preistext, waehrung)
        except AdapterFehler as error:
            raise ValidationError(str(error)) from error

        angebot = self.record_offer(
            line_id,
            int(shop["id"]),
            str(felder["produktname"]),
            seite.final_url,
            preis,
            felder.get("lieferzeit_text"),
            felder.get("lager_text"),
            felder.get("artikelnummer"),
            waehrung,
            None,
            f"adapter:{adapter.id}",
        )
        return {
            **angebot,
            # Was die Seite wörtlich gesagt hat - damit der Aufrufer die
            # Zuordnung prüfen kann, ohne der Engine glauben zu müssen.
            "extraktion": {
                "adapter": adapter.id,
                "final_url": seite.final_url,
                "felder": felder,
            },
        }

    @staticmethod
    def _pruefe_shopwaehrung(
        shop: Mapping[str, Any], waehrung: str, preistext: str
    ) -> None:
        """Eine Shopwährung, die dem Shopland widerspricht, braucht einen Beleg.

        ``record_shop`` setzt CHF, wenn keine Währung angegeben wird. Bei einem
        Shop ausserhalb des CHF-Raums ist CHF deshalb genauso oft ein Versehen
        wie eine Tatsache - und ein als CHF verbuchter Europreis wäre still
        falsch, mit Kurs 1 und ohne jeden Beleg. Sagt die Seite die Währung
        selbst, ist die Sache entschieden; sonst wird nichts erfasst.
        """
        land = str(shop.get("land") or "").strip().upper()
        landeswaehrung = waehrung_fuer_land(land)
        if landeswaehrung is None or landeswaehrung == waehrung:
            return
        if nennt_waehrung(preistext, waehrung):
            return
        raise ValidationError(
            f"Shop {shop['id']} liegt in {land}, ist aber in {waehrung} geführt, "
            f"und der Preistext «{preistext}» nennt keine Währung. Ohne Beleg "
            "wird nichts erfasst - Shopwährung korrigieren oder record_offer "
            "mit ausdrücklicher Währung verwenden."
        )

    def record_manual_offer(
        self,
        line_id: int,
        produkt_url: str,
        produktname: str,
        preis: Any,
        waehrung: str = HOME_CURRENCY,
        lieferzeit_text: str | None = None,
        lager_text: str | None = None,
        artikelnummer: str | None = None,
    ) -> dict[str, Any]:
        """Ein von Hand abgetipptes Angebot erfassen - der «ungeprüft»-Weg.

        Der Shop wird über die Domain der URL gefunden, genau wie bei
        :meth:`fetch_offer`. ``erfasst_via`` bleibt leer: hier hat niemand
        ausser dem Menschen die Seite gelesen, und die Oberfläche weist das
        auch so aus. Ist der Shop unbekannt, kommt derselbe Klartexthinweis
        wie beim Abruf - erfasst wird nichts.
        """
        shop = self._shop_fuer_domain(_hostname(produkt_url, "Produkt-URL"))
        return self.record_offer(
            line_id,
            int(shop["id"]),
            produktname,
            produkt_url,
            preis,
            lieferzeit_text,
            lager_text,
            artikelnummer,
            waehrung,
        )

    def refresh_offer(self, offer_id: int) -> dict[str, Any]:
        """Ein bestehendes Angebot noch einmal von der Produktseite lesen.

        Kein Sonderpfad: gelesen und geschrieben wird über
        :meth:`fetch_offer`, mit denselben Prüfungen - Adapter, robots.txt,
        Mindestabstand, Shopwährung, Weiterleitungsregel.

        Die Historie ist **tagesgenau** (Migration 004). Ein zweiter Refresh am
        selben Tag überschreibt die heutige Beobachtung; ``vorher`` in der
        Antwort ist dann der einzige Ort, an dem der frühere Wert noch steht.
        Eine Historie innerhalb eines Tages gibt es nicht.

        Scheitert der Abruf, bleibt die Datenbank unangetastet: die alte
        Beobachtung steht weiter, wird älter und fällt irgendwann ehrlich aus
        dem 14-Tage-Fenster. Ein gescheiterter Refresh entwertet nie Daten.
        """
        vorher = self.repository.get_offer(offer_id)
        if vorher is None:
            raise ValidationError(f"Angebot {offer_id} ist unbekannt")
        ergebnis = dict(
            self.fetch_offer(int(vorher["line_id"]), str(vorher["produkt_url"]))
        )
        extraktion = ergebnis.pop("extraktion", None)
        alt = {
            "preis_chf": vorher.get("preis_chf"),
            "lieferzeit_tage": vorher.get("lieferzeit_tage"),
            "lager_text": vorher.get("lager_text"),
            "beobachtungstag": vorher.get("beobachtungstag"),
        }
        return {
            "vorher": alt,
            "nachher": ergebnis,
            "geaendert": any(
                not _gleicher_wert(alt[feld], ergebnis.get(feld))
                for feld in ("preis_chf", "lieferzeit_tage", "lager_text")
            ),
            "extraktion": extraktion,
        }

    def refreshable_offers(self, job_id: int) -> list[dict[str, Any]]:
        """Die Angebote, die ein «Preise prüfen» anfassen würde.

        Genau die Menge, mit der auch der Optimierer rechnet: jüngste
        Beobachtung je Zeile×URL, Zeile noch offen, Shop nicht gesperrt. Ob ein
        Adapter greift, wird ohne jeden Netzzugriff beantwortet - die Liste
        selbst ruft nirgends an.
        """
        data = self.repository.optimization_input(job_id)
        offen = {int(value) for value in data.get("required_line_ids", [])}
        eintraege = [
            {
                "offer_id": int(row["id"]),
                "line_id": int(row["line_id"]),
                "position": int(row.get("position") or 0),
                "produktname": row.get("produktname"),
                "produkt_url": row.get("produkt_url"),
                "preis_chf": str(row["preis_chf"]),
                "shop_id": int(row["shop_id"]),
                "adapter_verfuegbar": _adapter_deckt(row.get("produkt_url")),
            }
            for row in data.get("offers", [])
            if int(row["line_id"]) in offen
        ]
        return sorted(
            eintraege, key=lambda row: (row["position"], str(row["produkt_url"]))
        )

    def list_adapters(self) -> dict[str, Any]:
        """Geladene Adapter und übersprungene Dateien - read-only."""
        return adapter_uebersicht()

    def _shop_fuer_domain(self, domain: str) -> dict[str, Any]:
        """Shop zur Domain einer Produkt-URL, nach derselben Regel wie record_offer."""
        treffer = [
            shop
            for shop in self.repository.list_shops()
            if _deckt_domain(domain, str(shop["domain"]).lower().removeprefix("www."))
        ]
        if not treffer:
            raise ValidationError(
                f"Kein Shop mit Domain {domain} bekannt; zuerst record_shop aufrufen"
            )
        # Die spezifischere Domain gewinnt, bei Gleichstand die kleinere ID.
        return sorted(
            treffer, key=lambda shop: (-len(str(shop["domain"])), int(shop["id"]))
        )[0]

    def _umrechnung(self, waehrung: str, preis_original: Decimal) -> dict[str, Any]:
        """Originalpreis in CHF überführen und die Umrechnung belegen.

        Bei CHF ist das ein No-op mit Kurs 1. Bei Fremdwährung holt der Server
        den Tageskurs und legt Kurs, Kursdatum und Quelle zum Angebot - ohne
        diese vier Felder lässt die Datenbank die Zeile ohnehin nicht zu.
        """
        code = (waehrung or HOME_CURRENCY).strip().upper()
        if code == HOME_CURRENCY:
            return {
                "preis_chf": preis_original,
                "kurs": None,
                "spalten": {
                    "preis_original": preis_original,
                    "waehrung": HOME_CURRENCY,
                    "kurs": Decimal("1"),
                    "kurs_am": None,
                    "kurs_quelle": None,
                },
            }
        try:
            kurs = aktueller_kurs(self.repository, code, self._heute())
        except KursError as error:
            raise ValidationError(str(error)) from error
        return {
            "preis_chf": nach_chf(preis_original, kurs.kurs),
            "kurs": kurs,
            "spalten": {
                "preis_original": preis_original,
                "waehrung": code,
                "kurs": kurs.kurs,
                "kurs_am": kurs.geholt_am,
                "kurs_quelle": kurs.quelle_url,
            },
        }

    @staticmethod
    def _heute() -> date:
        return datetime.now(timezone.utc).date()

    def mark_line(
        self,
        line_id: int,
        status: str,
        kommentar: str | None = None,
        stock_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        if self.repository.get_line(line_id) is None:
            raise ValidationError(f"Zeile {line_id} ist unbekannt")
        allowed = {"bestand", "nichts_gefunden", "erledigt"}
        if status not in allowed:
            raise ValidationError(f"Status muss einer von {sorted(allowed)} sein")
        if stock_ids is not None and status != "bestand":
            raise ValidationError("stock_ids ist nur mit Status Bestand zulässig")
        try:
            return self.repository.mark_line(line_id, status, kommentar, stock_ids)
        except ValueError as error:
            raise ValidationError(str(error)) from error

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
            "only_ch": "Nur Schweiz",
        }
        named_presets = [
            (key, presets[key])
            for key in ("cheapest", "fastest", "one_shop", "balanced", "only_ch")
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
                "provenienz_text": row.get("provenienz_text"),
                # Leer heisst «von Hand bzw. via KI erfasst» - die Oberfläche
                # weist genau das als ungeprüft aus.
                "erfasst_via": row.get("erfasst_via"),
                "pinned": row.get("override_status") == "pin",
                "excluded": offer_id in excludes,
                # Auch die Kandidatenzeilen tragen Originalbetrag, Umrechnung
                # und Beleg - dort schaut man beim Vergleichen hin.
                "lieferziel_name": shop.get("lieferziel_name"),
                "abholung": str(shop.get("lieferziel_land") or "CH").upper() != "CH",
                **cls._waehrungs_felder(row),
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
        canonical_variant = matches[0]
        if canonical_variant.get("contains_unknown_shipping"):
            raise ValidationError(
                "Bestellung kann nicht erfasst werden: Versandkosten sind unbekannt"
            )
        shop_keys = {str(shop_id) for shop_id in canonical_variant["shop_ids"]}
        if set(zugesagt_liefertage_pro_shop) != shop_keys or any(
            not isinstance(days, int) or days <= 0
            for days in zugesagt_liefertage_pro_shop.values()
        ):
            raise ValidationError("Liefertage muessen fuer jeden Shop positiv angegeben sein")
        return self.repository.create_purchase(
            job_id, canonical_variant, ordered_at, zugesagt_liefertage_pro_shop
        )

    # -- Warenkorb-Übergabe -------------------------------------------------

    PLATFORM_LABELS = {
        "opencart": "OpenCart",
        "woocommerce": "WooCommerce",
        "shopify": "Shopify",
    }

    def cart_shops(self, job_id: int, tempo: float = 0.5) -> list[dict[str, Any]]:
        """Shops des gewählten Plans samt Eignung für die Warenkorb-Übergabe."""
        _, variant, shop_rows = self._selected_plan(job_id, tempo)
        result = []
        for shop in variant["shops"]:
            row = shop_rows.get(int(shop["id"]), {})
            plattform = row.get("plattform")
            geprueft = row.get("plattform_geprueft_am") is not None
            result.append(
                {
                    "shop_id": int(shop["id"]),
                    "shop_name": shop["name"],
                    "shop_url": shop.get("url"),
                    "plattform": plattform,
                    "plattform_geprueft": geprueft,
                    # Der Knopf erscheint, solange die Plattform unterstützt ist
                    # oder noch nie abschliessend geprüft wurde. Ein geprüfter,
                    # nicht unterstützter Shop bekommt keinen toten Knopf.
                    "kann_fuellen": plattform in SUPPORTED_PLATFORMS or not geprueft,
                }
            )
        return result

    def fill_cart(
        self,
        job_id: int,
        shop_id: int,
        *,
        tempo: float = 0.5,
        session_factory: Any = None,
        stub: str | None = None,
    ) -> dict[str, Any]:
        """Gast-Warenkorb für genau einen Shop des gewählten Plans füllen.

        Ein Knopfdruck deckt beides ab: ergibt die Erkennung eine unterstützte
        Plattform, läuft derselbe Versuch direkt in Füllen und Rückverifikation
        weiter. Ein zweiter Klick ist nicht nötig.

        ``stub`` schaltet auf eine Shop-Attrappe für den E2E-Klickpfad um. Dieser
        Weg schreibt bewusst nichts: der Testlauf soll weder eine Plattform
        festschreiben noch Produkt-IDs echter Angebote überschreiben.
        """
        shop = self.repository.get_shop(shop_id)
        if shop is None:
            raise ValidationError(f"Shop {shop_id} ist unbekannt")
        shop_url = shop.get("url")
        if not shop_url:
            raise ValidationError(f"Shop {shop_id} hat keine URL")

        _, variant, _ = self._selected_plan(job_id, tempo)
        items = self._cart_items(job_id, variant, shop_id)
        if not items:
            raise ValidationError(
                "Der gewählte Bestellplan enthält für diesen Shop keine Position"
            )

        if stub:
            session = build_stub_session(items, mismatch=stub == "mismatch")
        else:
            session = (session_factory or open_session)()
        persist = not stub

        plattform = shop.get("plattform")
        beleg = shop.get("plattform_beleg")

        if shop.get("plattform_geprueft_am") is None:
            # Wirft CartTemporaryError bei Timeout/Netzfehler - dann wird
            # bewusst nichts geschrieben und der Knopf bleibt stehen.
            evidence = start_guest_session(session, shop_url)
            plattform = evidence.plattform if evidence else None
            beleg = (
                evidence.beleg
                if evidence
                else "Geprüft: keine Merkmale von OpenCart, WooCommerce oder Shopify gefunden"
            )
            if persist:
                self.repository.save_shop_platform(shop_id, plattform, beleg)
        else:
            # Plattform steht schon fest; der Landing-Aufruf eröffnet nur noch
            # die Gast-Session.
            start_guest_session(session, shop_url)

        if plattform not in SUPPORTED_PLATFORMS:
            # Erwartbarer Ausgang, kein Fehler: die Linkliste bleibt der Weg.
            benannt = self.PLATFORM_LABELS.get(plattform or "", "keine bekannte Plattform")
            return {
                "status": "nicht_unterstuetzt",
                "job_id": job_id,
                "shop_id": shop_id,
                "shop_name": shop.get("name"),
                "shop_url": shop_url,
                "plattform": plattform,
                "plattform_beleg": beleg,
                "text": (
                    f"Plattform geprüft: {benannt} – für diesen Shop bleibt die Bestellliste."
                ),
            }

        adapter = build_adapter(plattform, session)
        fill = adapter.fill(shop_url, items)
        if persist:
            if fill.produkt_ids:
                self.repository.save_offer_product_ids(fill.produkt_ids)
            if fill.artikelnummern:
                self.repository.save_offer_artikelnummern(fill.artikelnummern)

        return {
            "status": "uebergabe",
            "job_id": job_id,
            "shop_id": shop_id,
            "shop_name": shop.get("name"),
            "shop_url": shop_url,
            "plattform": fill.plattform,
            "plattform_beleg": beleg,
            "verifiziert": fill.verifiziert,
            "artikel_anzahl": fill.artikel_anzahl,
            "total_chf": str(fill.total_chf.quantize(Decimal("0.01"))),
            "positionen": fill.positionen,
            "cookie": (
                None
                if not fill.cookie_wert
                else {"name": fill.cookie_name, "wert": fill.cookie_wert}
            ),
            "cart_url": fill.cart_url,
            # Ziel der Ein-Klick-Übergabe, plattformspezifisch. Bei OpenCart die
            # Korbseite, damit der gefüllte Korb sofort sichtbar ist.
            "uebergabe_url": fill.uebergabe_url,
        }

    def _selected_plan(
        self, job_id: int, tempo: float
    ) -> tuple[dict[str, Any], dict[str, Any], dict[int, dict[str, Any]]]:
        """Den serverseitig persistierten Plan auflösen - genau der wird gefüllt."""
        matrix = self.plan_scenarios(job_id, tempo=tempo)
        target = matrix.get("selected_assignments")
        candidates = [
            *matrix.get("scenarios", []),
            *([matrix["custom"]] if matrix.get("custom") else []),
        ]
        variant = next(
            (item for item in candidates if item["assignments"] == target), None
        )
        if variant is None:
            raise ValidationError(
                "Für diesen Job ist kein gültiger Bestellplan gewählt"
            )
        shop_rows = {
            int(row["id"]): row
            for row in self.repository.optimization_input(job_id).get("shops", [])
        }
        return matrix, variant, shop_rows

    def _cart_items(
        self, job_id: int, variant: dict[str, Any], shop_id: int
    ) -> list[CartItem]:
        data = self.repository.optimization_input(job_id)
        cached = {
            int(row["id"]): (row.get("shop_produkt_id"), row.get("artikelnummer"))
            for row in data.get("offers", [])
        }
        return [
            CartItem(
                line_id=int(line["line_id"]),
                offer_id=int(line["offer_id"]),
                produktname=line.get("produktname") or "",
                produkt_url=line.get("produkt_url") or "",
                menge=int(line["menge"]),
                einzelpreis_chf=Decimal(str(line["einzelpreis_chf"])),
                shop_produkt_id=cached.get(int(line["offer_id"]), (None, None))[0],
                artikelnummer=cached.get(int(line["offer_id"]), (None, None))[1],
            )
            for line in variant.get("lines", [])
            if int(line["shop_id"]) == shop_id
        ]

    #: Symbole für die Originalbeträge; unbekannte Währungen zeigen ihren Code.
    WAEHRUNGSSYMBOLE = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}

    @staticmethod
    def _waehrungs_felder(row: Mapping[str, Any]) -> dict[str, Any]:
        """Originalbetrag und Umrechnung mit Beleg, wie bei Lieferzeit-Texten.

        Bei CHF gibt es nichts zu zeigen - dann bleibt die Zeile so knapp wie
        bisher.
        """
        code = str(row.get("waehrung") or HOME_CURRENCY).upper()
        if code == HOME_CURRENCY or row.get("preis_original") is None:
            return {"waehrung": HOME_CURRENCY, "waehrung_fremd": False, "waehrung_beleg": None}
        symbol = ProcurementService.WAEHRUNGSSYMBOLE.get(code, code)
        original = Decimal(str(row["preis_original"]))
        kurs = row.get("kurs")
        kurs_am = row.get("kurs_am")
        quelle = str(row.get("kurs_quelle") or "")
        quelle_kurz = "EZB" if "frankfurter" in quelle else "Quelle"
        beleg = None
        if kurs is not None and kurs_am is not None:
            tag = kurs_am if isinstance(kurs_am, str) else f"{kurs_am:%d.%m.}"
            beleg = f"Kurs {Decimal(str(kurs)):.4f} ({quelle_kurz}, {tag})"
        return {
            "waehrung": code,
            "waehrung_fremd": True,
            "preis_original": str(original),
            "preis_original_text": f"{symbol} {original}",
            "kurs": None if kurs is None else str(kurs),
            "kurs_quelle": quelle or None,
            "waehrung_beleg": beleg,
        }

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
                "versand_original": (
                    None
                    if shop_rows[shop_id].get("versand_original") is None
                    else str(shop_rows[shop_id]["versand_original"])
                ),
                "gratis_ab_original": (
                    None
                    if shop_rows[shop_id].get("gratis_ab_original") is None
                    else str(shop_rows[shop_id]["gratis_ab_original"])
                ),
                "mindestbestellwert_original": (
                    None
                    if shop_rows[shop_id].get("mindestbestellwert_original") is None
                    else str(shop_rows[shop_id]["mindestbestellwert_original"])
                ),
                "versand_waehrung": str(
                    shop_rows[shop_id].get("versand_waehrung") or HOME_CURRENCY
                ).upper(),
                "versand_kurs": (
                    None
                    if shop_rows[shop_id].get("versand_kurs") is None
                    else str(shop_rows[shop_id]["versand_kurs"])
                ),
                "versand_kurs_am": (
                    None
                    if shop_rows[shop_id].get("versand_kurs_am") is None
                    else str(shop_rows[shop_id]["versand_kurs_am"])
                ),
                "versand_kurs_quelle": shop_rows[shop_id].get("versand_kurs_quelle"),
                "artikelanzahl": assignment_shop_counts.get(shop_id, 0),
                "versand_gratis": (
                    variant["shipping"][str(shop_id)] is not None
                    and Decimal(variant["shipping"][str(shop_id)]) == 0
                ),
                "versand_unbekannt": variant["shipping"][str(shop_id)] is None,
                "lieferziel_name": shop_rows[shop_id].get("lieferziel_name"),
                "lieferziel_land": str(shop_rows[shop_id].get("lieferziel_land") or "CH").upper(),
                "abholung": str(shop_rows[shop_id].get("lieferziel_land") or "CH").upper() != "CH",
            }
            for shop_id in variant["shop_ids"]
        ]
        variant["shops"].sort(
            key=lambda row: Decimal(row["subtotal_chf"]), reverse=True
        )
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
                    "erfasst_via": row.get("erfasst_via"),
                    "pinned": row.get("override_status") == "pin",
                    # Abholung: Shops mit fremdem Ziel tragen den Vermerk mit.
                    "lieferziel_name": shop.get("lieferziel_name"),
                    "lieferziel_land": str(shop.get("lieferziel_land") or "CH").upper(),
                    "abholung": str(shop.get("lieferziel_land") or "CH").upper() != "CH",
                    **ProcurementService._waehrungs_felder(row),
                }
            )
        variant["lines"] = sorted(lines, key=lambda row: (row["position"], row["line_id"]))
        max_days = variant.get("max_liefertage")
        max_lines = [
            row for row in lines
            if max_days is not None and row["lieferzeit_tage"] == max_days
        ]
        variant["max_delivery_only_estimated"] = bool(max_lines) and all(
            row["lieferzeit_geschaetzt"] for row in max_lines
        )
        # Wertfreigrenzen-Indikator pro Nicht-Heim-Ziel. Reine Anzeige: der Wert
        # fliesst nirgends in ein Total ein, und es wird keine Steuer berechnet.
        einfuhr: list[dict[str, Any]] = []
        for shop_id in variant["shop_ids"]:
            shop_row = shop_rows[int(shop_id)]
            if str(shop_row.get("lieferziel_land") or "CH").upper() == "CH":
                continue
            brutto = sum(
                (
                    Decimal(row["einzelpreis_chf"]) * row["menge"]
                    for row in variant["lines"]
                    if int(row["shop_id"]) == int(shop_id)
                ),
                Decimal("0.00"),
            )
            eintrag = next(
                (item for item in einfuhr if item["lieferziel_id"] == shop_row["lieferziel_id"]),
                None,
            )
            if eintrag is None:
                eintrag = {
                    "lieferziel_id": shop_row["lieferziel_id"],
                    "name": shop_row.get("lieferziel_name"),
                    "land": str(shop_row.get("lieferziel_land")).upper(),
                    "brutto_chf": Decimal("0.00"),
                }
                einfuhr.append(eintrag)
            eintrag["brutto_chf"] += brutto
        for eintrag in einfuhr:
            netto = (eintrag["brutto_chf"] / AUSLAND_MWST).quantize(Decimal("0.01"))
            ueber = netto > WERTFREIGRENZE_CHF
            eintrag["brutto_chf"] = str(eintrag["brutto_chf"])
            eintrag["netto_ca_chf"] = str(netto)
            eintrag["ueber_freigrenze"] = ueber
            eintrag["text"] = (
                f"{eintrag['land']}-Anteil netto ≈ CHF {netto} — über der Wertfreigrenze "
                f"von CHF {WERTFREIGRENZE_CHF}; bei Einfuhr 8.1 % MwSt auf den Gesamtwert"
                if ueber
                else
                f"{eintrag['land']}-Anteil netto ≈ CHF {netto} — unter der Wertfreigrenze "
                f"von CHF {WERTFREIGRENZE_CHF}"
            )
        variant["einfuhr"] = einfuhr
        variant["enthaelt_abholung"] = bool(variant.get("aufschlaege"))

        variant["missing_lines"] = [
            {
                "line_id": line_id,
                "position": line_rows.get(line_id, {}).get("position"),
                "suchtext": line_rows.get(line_id, {}).get("suchtext"),
            }
            for line_id in variant.get("missing_line_ids", [])
        ]
        if variant["missing_lines"]:
            offen = ", ".join(
                f"Position {row['position']}: {row['suchtext']}"
                for row in variant["missing_lines"]
            )
            variant["deckt_nicht_ab"] = f"deckt {offen} nicht ab"
        else:
            variant["deckt_nicht_ab"] = None
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
                versand_chf=(
                    None
                    if row.get("versand_chf") is None
                    else Decimal(str(row["versand_chf"]))
                ),
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
                lieferziel_id=(
                    None if row.get("lieferziel_id") is None else int(row["lieferziel_id"])
                ),
                lieferziel_name=row.get("lieferziel_name"),
                aufschlag_chf=Decimal(str(row.get("lieferziel_aufschlag_chf") or "0.00")),
                zuschlag_tage=int(row.get("lieferziel_zuschlag_tage") or 0),
                # Ohne Zielangabe gilt Heimat - so verhalten sich Bestandsdaten
                # wie vor der Einfuehrung der Lieferziele.
                ist_heimat=str(row.get("lieferziel_land") or "CH").upper() == "CH",
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
            "shipping": {
                str(key): (None if value is None else str(value))
                for key, value in variant.shipping.items()
            },
            "total_chf": str(variant.total_chf),
            "max_liefertage": variant.max_liefertage,
            "score": str(variant.score),
            "contains_estimates": variant.contains_estimates,
            "contains_unknown_delivery": variant.contains_unknown_delivery,
            "contains_unknown_shipping": variant.contains_unknown_shipping,
            "missing_line_ids": list(variant.missing_line_ids),
            "aufschlaege": [
                {"lieferziel_id": ziel_id, "name": name, "betrag_chf": str(betrag)}
                for ziel_id, name, betrag in variant.aufschlaege
            ],
            "aufschlag_chf": str(variant.aufschlag_chf),
        }
