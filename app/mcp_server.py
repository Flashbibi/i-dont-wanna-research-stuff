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
        lieferzeit_default_tage: int,
    ) -> dict[str, Any]:
        """Schweizer Shop vor seinem ersten Angebot als ungeprueft erfassen."""
        return service.record_shop(
            name,
            url,
            land,
            versand_chf,
            gratis_ab_chf,
            mindestbestellwert_chf,
            lieferzeit_default_tage,
        )

    @mcp.tool()
    def record_offer(
        line_id: int,
        shop_id: int,
        produktname: str,
        produkt_url: str,
        preis_chf: float,
        lieferzeit_tage: int | None = None,
        lager: str | None = None,
    ) -> dict[str, Any]:
        """Auf der Produktseite verifiziertes CHF-Angebot erfassen."""
        return service.record_offer(
            line_id,
            shop_id,
            produktname,
            produkt_url,
            preis_chf,
            lieferzeit_tage,
            lager,
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
