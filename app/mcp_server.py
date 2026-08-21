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
    def delete_job(job_id: int, confirm_job_id: int) -> dict[str, Any]:
        """Exakt bestätigten, unberührten echten Job löschen. Erlaubt nur bei Status offen, ausschließlich offenen Zeilen und ohne Angebote oder Kauf; job_id und confirm_job_id müssen identisch sein."""
        return service.delete_job(job_id, confirm_job_id)

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
    def get_shops() -> list[dict[str, Any]]:
        """Alle bekannten Shops mit shop_id, Name, URL, Status und vorhandenen Versandprofildaten deterministisch sortiert lesen (read-only)."""
        return service.get_shops()

    @mcp.tool()
    def plan_scenarios(
        job_id: int,
        tempo: float = 0.5,
        pins: dict[int | str, int] | None = None,
        excludes: list[int] | None = None,
    ) -> dict[str, Any]:
        """Die vollständige Szenariomatrix mit optionalem Tempo, Pins und Excludes über exakt dieselbe Serverfunktion wie die Job-UI berechnen (read-only). Totale enthalten den Abhol-Aufschlag je beteiligtem Nicht-Heim-Lieferziel, einmal pro Ziel und Plan (Feld aufschlaege); Wartezeiten des Ziels stecken in den Lieferzeiten. Das Preset «Nur Schweiz» (only_ch) verschmilzt mit dem Gesamtoptimum, solange kein Auslandsangebot gewinnt, und erscheint unvollständig mit deckt_nicht_ab, wenn der Heimmarkt nicht jede Zeile abdeckt. Das Feld einfuhr ist ein reiner Anzeige-Indikator zur Wertfreigrenze - es wird keine Steuer berechnet und nichts davon fliesst in ein Total."""
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
    def check_stock(line_id: int) -> dict[str, Any]:
        """Passende Bestände und ähnliche Kandidaten für eine Zeile prüfen (read-only)."""
        return service.check_stock(line_id)

    @mcp.tool()
    def adjust_stock(stock_id: int, delta: int, kommentar: str) -> dict[str, Any]:
        """Bestandsmenge mit begründeter Korrekturbuchung anpassen."""
        return service.korrigiere_bestand(stock_id, delta, kommentar)

    @mcp.tool()
    def record_shop(
        name: str,
        url: str,
        land: str,
        versand_chf: float | None,
        gratis_ab_chf: float | None,
        mindestbestellwert_chf: float | None,
        lieferzeit_default_tage: int | None,
        profil_quelle_url: str,
        versand_text: str,
        lieferziel_id: int | None = None,
        waehrung: str = "CHF",
    ) -> dict[str, Any]:
        """Shop mit tatsächlichem Herkunftsland, explizitem Lieferziel, HTTP(S)-Profilquelle und Versand-Originaltext erfassen. Shopland und Lieferziel dürfen verschieden sein; ohne lieferziel_id wird nur bei genau einer Adresse im Shopland abgeleitet. Bei waehrung != CHF tragen versand_chf, gratis_ab_chf und mindestbestellwert_chf die Originalbeträge; der Server rechnet sie mit belegtem Tageskurs um. Unbekannte Versandkosten werden als null erfasst, niemals als kostenlos."""
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
            lieferziel_id,
            waehrung,
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
        waehrung: str = "CHF",
        provenienz_text: str | None = None,
    ) -> dict[str, Any]:
        """Angebot einer bekannten Zeile bei einem nicht gesperrten Shop erfassen oder die heutige Beobachtung aktualisieren; URL, Preis und wörtliche Liefer-/Lagertexte werden validiert. Die optionale shopinterne Artikelnummer ankert die Warenkorb-Prüfung sprachunabhängig; ohne sie zieht der Adapter sie beim ersten Füllen selbst von der Produktseite. Bei waehrung != CHF ist preis_chf der Preis in DIESER Währung; der Server rechnet selbst mit dem belegten Tageskurs in CHF um und legt Kurs, Kursdatum und Quelle dazu - niemals selbst umrechnen. Bei Marktplätzen nennt provenienz_text den sichtbaren Verkäufer und die Versandpartei wörtlich."""
        return service.record_offer(
            line_id,
            shop_id,
            produktname,
            produkt_url,
            preis_chf,
            lieferzeit_text,
            lager_text,
            artikelnummer,
            waehrung,
            provenienz_text,
        )

    @mcp.tool()
    def mark_line(
        line_id: int,
        status: str,
        kommentar: str | None = None,
        stock_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Zeile als Bestand, nichts gefunden oder erledigt markieren; bei Bestand wird die benötigte Menge aus dem Lager abgebucht."""
        return service.mark_line(line_id, status, kommentar, stock_ids)

    @mcp.tool()
    def plan_order(job_id: int, tempo: float) -> list[dict[str, Any]]:
        """Bis zu drei Bestellvarianten aus den neuesten Angeboten nicht gesperrter Shops berechnen; Pins, Excludes, Versand, Mindestwerte, Lieferzeiten, Tempo, Unbekannt-Malus und der Abhol-Aufschlag je Nicht-Heim-Lieferziel werden berücksichtigt."""
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
