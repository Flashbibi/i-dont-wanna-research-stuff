from __future__ import annotations

import warnings
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .procurement import ProcurementService


def build_mcp(service: ProcurementService) -> FastMCP:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "192.168.1.60:*",
            "192.168.1.60",
            "127.0.0.1:*",
            "localhost:*",
            "testserver",
        ],
        allowed_origins=[
            "http://192.168.1.60:*",
            "http://127.0.0.1:*",
            "http://localhost:*",
        ],
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mcp = FastMCP(
            "beschaffung",
            instructions="Validiertes Beschaffungsgedaechtnis fuer Schweizer Shops.",
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=security,
        )

    @mcp.tool()
    def create_job(zeilen: list[str]) -> dict[str, Any]:
        """Echten UI-Job aus einer Zeilenliste anlegen; Mengenpräfixe wie «2x …» werden über denselben Parser wie die Textarea verarbeitet."""
        return service.create_job_from_lines(zeilen)

    @mcp.tool()
    def get_job(job_id: int) -> dict[str, Any]:
        """Jobstatus und Zeilenstatus samt Kandidatenzahl lesen sowie mit der UI-Szenariologik prüfen, ob Szenarien verfügbar sind (read-only)."""
        return service.get_job(job_id)

    @mcp.tool()
    def search_history(text: str) -> list[dict[str, Any]]:
        """Käufe nach Produktname oder Shop suchen; Bestellzeit, Preis, zugesagte Liefertage und Ankunft lesen (read-only)."""
        return service.search_history(text)

    @mcp.tool()
    def get_stock() -> list[dict[str, Any]]:
        """Aktuell positiven Bestand lesen (read-only)."""
        return service.get_stock()

    @mcp.tool()
    def plan_scenarios(
        job_id: int,
        tempo: float = 0.5,
        pins: dict[int | str, int] | None = None,
        excludes: list[int] | None = None,
    ) -> dict[str, Any]:
        """Die vollständige Szenariomatrix mit optionalem Tempo, Pins und Excludes über exakt dieselbe Serverfunktion wie die Job-UI berechnen (read-only)."""
        return service.plan_scenarios(
            job_id, tempo=tempo, pins=pins, excludes=excludes
        )

    @mcp.tool()
    def next_job() -> dict[str, Any] | None:
        """Ältesten nicht als Test markierten offenen Job mit seinen noch offenen oder in Arbeit befindlichen Zeilen laden."""
        return service.next_job()

    @mcp.tool()
    def check_line(line_id: int) -> dict[str, Any]:
        """Exakten Bestand, frühere Käufe und höchstens 14 Tage alte Angebote für eine Zeile gemeinsam laden (read-only)."""
        return service.check_line(line_id)

    @mcp.tool()
    def record_shop(
        name: str,
        url: str,
        land: str,
        versand_chf: float,
        gratis_ab_chf: float | None,
        mindestbestellwert_chf: float | None,
        lieferzeit_default_tage: int | None,
        profil_quelle_url: str,
        versand_text: str,
    ) -> dict[str, Any]:
        """Neuen Schweizer Shop mit angegebener HTTP(S)-Profilquelle, Versand-Originaltext und validierten Profilwerten erfassen."""
        return service.record_shop(
            name,
            url,
            land,
            versand_chf,
            gratis_ab_chf,
            mindestbestellwert_chf,
            lieferzeit_default_tage,
            profil_quelle_url,
            versand_text,
        )

    @mcp.tool()
    def record_offer(
        line_id: int,
        shop_id: int,
        produktname: str,
        produkt_url: str,
        preis_chf: float,
        lieferzeit_text: str | None = None,
        lager_text: str | None = None,
        artikelnummer: str | None = None,
    ) -> dict[str, Any]:
        """Angebot einer bekannten Zeile bei einem nicht gesperrten Shop erfassen oder die heutige Beobachtung aktualisieren; URL, Preis und wörtliche Liefer-/Lagertexte werden validiert. Die optionale shopinterne Artikelnummer ankert die Warenkorb-Prüfung sprachunabhängig; ohne sie zieht der Adapter sie beim ersten Füllen selbst von der Produktseite."""
        return service.record_offer(
            line_id,
            shop_id,
            produktname,
            produkt_url,
            preis_chf,
            lieferzeit_text,
            lager_text,
            artikelnummer,
        )

    @mcp.tool()
    def mark_line(
        line_id: int, status: str, kommentar: str | None = None
    ) -> dict[str, Any]:
        """Zeile als Bestand, nichts gefunden oder erledigt markieren; bei Bestand wird die benötigte Menge aus dem Lager abgebucht."""
        return service.mark_line(line_id, status, kommentar)

    @mcp.tool()
    def plan_order(job_id: int, tempo: float) -> list[dict[str, Any]]:
        """Bis zu drei Bestellvarianten aus den neuesten Angeboten nicht gesperrter Shops berechnen; Pins, Excludes, Versand, Mindestwerte, Lieferzeiten, Tempo und Unbekannt-Malus werden berücksichtigt."""
        return service.plan_order(job_id, tempo)

    @mcp.tool()
    def get_cart_session(job_id: int, shop_id: int) -> dict[str, Any]:
        """Gast-Warenkorb beim Shop füllen und die Session übergeben: legt beim Shop eine Gast-Session an, füllt sie mit der persistierten Szenarioauswahl für diesen Shop und liest den Korb zurück; nur bei exakter Übereinstimmung von Artikelzahl und Zwischensumme wird übergeben, sonst kommt ein Fehler mit Diff. Kein Login, kein Kaufabschluss. Einziger DB-Schreibzugriff sind der Plattform-Befund und der Produkt-ID-Cache."""
        return service.fill_cart(job_id, shop_id)

    @mcp.tool()
    def record_purchase(
        job_id: int,
        variante: dict[str, Any],
        bestellt_am: str,
        zugesagt_liefertage_pro_shop: dict[str, int],
    ) -> dict[str, Any]:
        """Tatsächlich ausgelöste vollständige Bestellung nach erneuter serverseitiger Planvalidierung samt zugesagten Liefertagen speichern und den Job auf bestellt setzen."""
        return service.record_purchase(
            job_id,
            variante,
            bestellt_am,
            zugesagt_liefertage_pro_shop,
        )

    return mcp
