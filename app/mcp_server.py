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
        """Einen echten Job aus Positionen anlegen; Mengenpräfixe wie «2x …» werden geparst."""
        return service.create_job_from_lines(zeilen)

    @mcp.tool()
    def get_job(job_id: int) -> dict[str, Any]:
        """Jobstatus, Zeilen mit Kandidatenzahl und Szenarioverfügbarkeit lesen."""
        return service.get_job(job_id)

    @mcp.tool()
    def search_history(text: str) -> list[dict[str, Any]]:
        """Kaufhistorie read-only nach Produktname oder Shop durchsuchen."""
        return service.search_history(text)

    @mcp.tool()
    def get_stock() -> list[dict[str, Any]]:
        """Aktuell positiven Bestand read-only lesen."""
        return service.get_stock()

    @mcp.tool()
    def plan_scenarios(
        job_id: int,
        tempo: float = 0.5,
        pins: dict[int | str, int] | None = None,
        excludes: list[int] | None = None,
    ) -> dict[str, Any]:
        """Dieselbe serverseitige Szenariomatrix wie die Job-UI read-only berechnen."""
        return service.plan_scenarios(
            job_id, tempo=tempo, pins=pins, excludes=excludes
        )

    @mcp.tool()
    def next_job() -> dict[str, Any] | None:
        """Aeltesten offenen Job mit seinen offenen Zeilen laden."""
        return service.next_job()

    @mcp.tool()
    def check_line(line_id: int) -> dict[str, Any]:
        """Bestand, fruehere Kaeufe und frische Cache-Angebote gemeinsam laden."""
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
        """Nur gegen eine Profil-Quellseite geprüften Schweizer Shop erfassen."""
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
    ) -> dict[str, Any]:
        """Produktangebot mit wörtlicher Lieferzeit und Lagerstatus erfassen/aktualisieren."""
        return service.record_offer(
            line_id,
            shop_id,
            produktname,
            produkt_url,
            preis_chf,
            lieferzeit_text,
            lager_text,
        )

    @mcp.tool()
    def mark_line(
        line_id: int, status: str, kommentar: str | None = None
    ) -> dict[str, Any]:
        """Zeile als Bestand, nichts gefunden oder erledigt markieren."""
        return service.mark_line(line_id, status, kommentar)

    @mcp.tool()
    def plan_order(job_id: int, tempo: float) -> list[dict[str, Any]]:
        """Bis zu drei Varianten nur aus bestaetigten Angeboten berechnen."""
        return service.plan_order(job_id, tempo)

    @mcp.tool()
    def record_purchase(
        job_id: int,
        variante: dict[str, Any],
        bestellt_am: str,
        zugesagt_liefertage_pro_shop: dict[str, int],
    ) -> dict[str, Any]:
        """Tatsaechlich ausgeloeste Bestellung samt Zusagen speichern."""
        return service.record_purchase(
            job_id,
            variante,
            bestellt_am,
            zugesagt_liefertage_pro_shop,
        )

    return mcp
