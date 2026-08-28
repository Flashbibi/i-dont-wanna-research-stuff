import asyncio
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.mcp_server import build_mcp
from app.web import create_app


class StubService:
    def create_job_from_lines(self, lines):
        return {
            "job_id": 41,
            "lines": [
                {
                    "position": index,
                    "text": line.removeprefix("2x "),
                    "menge": 2 if line.startswith("2x ") else 1,
                }
                for index, line in enumerate(lines, 1)
            ],
        }

    def get_job(self, job_id):
        return {
            "id": job_id,
            "status": "in_arbeit",
            "lines": [],
            "scenarios_available": False,
        }

    def delete_job(self, job_id, confirm_job_id):
        return {"job_id": job_id, "confirm_job_id": confirm_job_id, "deleted": True}

    def search_history(self, text):
        return [{"produktname": text, "shop_name": "Swiss Shop"}]

    def get_stock(self):
        return [{"bezeichnung": "Servo", "menge": 3}]

    def get_shops(self):
        return [
            {
                "shop_id": 8,
                "name": "Reichelt",
                "url": "https://www.reichelt.de/",
                "lieferziel_id": 2,
            }
        ]

    def plan_scenarios(self, job_id, pins=None, excludes=None, tempo=0.5):
        return {
            "job_id": job_id,
            "tempo": tempo,
            "pins": pins or {},
            "excludes": excludes or [],
            "ready": True,
            "scenarios": [],
        }

    def next_job(self):
        return None

    def check_line(self, line_id):
        return {"line": {"id": line_id}}

    def check_stock(self, line_id):
        return {
            "line_id": line_id,
            "benoetigt": 2,
            "gedeckt": False,
            "fehlmenge": 2,
            "treffer": [],
            "kandidaten": [],
        }

    def korrigiere_bestand(self, stock_id, delta, kommentar):
        return {"id": stock_id, "menge": delta, "kommentar": kommentar}

    def record_shop(self, *args):
        return {"id": 1}

    def record_offer(self, *args, **kwargs):
        return {"id": 2, **kwargs}

    def fetch_offer(self, line_id, produkt_url):
        return {
            "id": 4,
            "line_id": line_id,
            "produkt_url": produkt_url,
            "erfasst_via": "adapter:demo",
            "extraktion": {"adapter": "demo", "final_url": produkt_url, "felder": {}},
        }

    def refresh_offer(self, offer_id):
        return {
            "vorher": {
                "preis_chf": "12.90",
                "lieferzeit_tage": 3,
                "lager_text": "an Lager",
                "beobachtungstag": "2026-08-24",
            },
            "nachher": {"id": offer_id, "preis_chf": "11.50"},
            "geaendert": True,
            "extraktion": {
                "adapter": "demo",
                "final_url": "https://shop.example/x",
                "felder": {},
            },
        }

    def list_adapters(self):
        return {
            "adapter": [
                {
                    "id": "demo",
                    "domain": "demoshop.example",
                    "felder": ["produktname", "preis"],
                    "quelle": "gebuendelt",
                    "datei": "demo.yaml",
                    "min_delay_s": 6.0,
                }
            ],
            "fehler": [],
        }

    def mark_line(self, *args, **kwargs):
        return {"id": args[0], "stock_ids": kwargs.get("stock_ids")}

    def plan_order(self, *args):
        return []

    def record_purchase(self, *args):
        return {"id": 3}

    def fill_cart(self, job_id, shop_id):
        return {
            "status": "uebergabe",
            "job_id": job_id,
            "shop_id": shop_id,
            "plattform": "opencart",
            "verifiziert": True,
            "artikel_anzahl": 3,
            "total_chf": "18.70",
            "positionen": [{"produktname": "Dupont", "menge": 2}],
            "cookie": {"name": "OCSESSID", "wert": "sess-1"},
            "cart_url": "https://shop.example.ch/index.php?route=checkout/cart",
        }


def test_mcp_exposes_exact_procurement_tools():
    server = build_mcp(StubService())

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "create_job",
        "delete_job",
        "get_job",
        "search_history",
        "get_stock",
        "get_shops",
        "plan_scenarios",
        "next_job",
        "check_line",
        "check_stock",
        "adjust_stock",
        "record_shop",
        "record_offer",
        "fetch_offer",
        "refresh_offer",
        "list_adapters",
        "mark_line",
        "plan_order",
        "record_purchase",
        "get_cart_session",
    }
    record_offer = next(tool for tool in tools if tool.name == "record_offer")
    properties = record_offer.inputSchema["properties"]
    assert "lieferzeit_text" in properties
    assert "lager_text" in properties
    assert "lieferzeit_tage" not in properties
    assert "lager" not in properties
    record_shop = next(tool for tool in tools if tool.name == "record_shop")
    shop_properties = record_shop.inputSchema["properties"]
    assert "waehrung" in shop_properties
    assert {entry.get("type") for entry in shop_properties["versand_chf"]["anyOf"]} == {
        "number",
        "null",
    }


def test_all_tool_descriptions_match_current_behavior():
    tools = asyncio.run(build_mcp(StubService()).list_tools())
    descriptions = {tool.name: tool.description for tool in tools}

    assert descriptions == {
        "create_job": (
            "Echten UI-Job aus einer Zeilenliste anlegen; Mengenpräfixe wie «2x …» "
            "werden über denselben Parser wie die Textarea verarbeitet."
        ),
        "delete_job": (
            "Exakt bestätigten, unberührten echten Job löschen. Erlaubt nur bei Status "
            "offen, ausschließlich offenen Zeilen und ohne Angebote oder Kauf; job_id "
            "und confirm_job_id müssen identisch sein."
        ),
        "get_job": (
            "Jobstatus und Zeilenstatus samt Kandidatenzahl lesen sowie mit der "
            "UI-Szenariologik prüfen, ob Szenarien verfügbar sind (read-only)."
        ),
        "search_history": (
            "Käufe nach Produktname oder Shop suchen; Bestellzeit, Preis, zugesagte "
            "Liefertage und Ankunft lesen (read-only)."
        ),
        "get_stock": "Aktuell positiven Bestand lesen (read-only).",
        "get_shops": (
            "Alle bekannten Shops mit shop_id, Name, URL, Status und vorhandenen "
            "Versandprofildaten deterministisch sortiert lesen (read-only)."
        ),
        "plan_scenarios": (
            "Die vollständige Szenariomatrix mit optionalem Tempo, Pins und Excludes "
            "über exakt dieselbe Serverfunktion wie die Job-UI berechnen (read-only). "
            "Totale enthalten den Abhol-Aufschlag je beteiligtem Nicht-Heim-Lieferziel, "
            "einmal pro Ziel und Plan (Feld aufschlaege); Wartezeiten des Ziels stecken "
            "in den Lieferzeiten. Das Preset «Nur Schweiz» (only_ch) verschmilzt mit dem "
            "Gesamtoptimum, solange kein Auslandsangebot gewinnt, und erscheint "
            "unvollständig mit deckt_nicht_ab, wenn der Heimmarkt nicht jede Zeile "
            "abdeckt. Das Feld einfuhr ist ein reiner Anzeige-Indikator zur "
            "Wertfreigrenze - es wird keine Steuer berechnet und nichts davon fliesst "
            "in ein Total."
        ),
        "next_job": (
            "Ältesten nicht als Test markierten offenen Job mit seinen noch offenen "
            "oder in Arbeit befindlichen Zeilen laden."
        ),
        "check_line": (
            "Exakten Bestand, frühere Käufe und höchstens 14 Tage alte Angebote für "
            "eine Zeile gemeinsam laden (read-only)."
        ),
        "check_stock": (
            "Passende Bestände und ähnliche Kandidaten für eine Zeile prüfen (read-only)."
        ),
        "adjust_stock": "Bestandsmenge mit begründeter Korrekturbuchung anpassen.",
        "record_shop": (
            "Shop mit tatsächlichem Herkunftsland, explizitem Lieferziel, HTTP(S)-"
            "Profilquelle und Versand-Originaltext erfassen. Shopland und Lieferziel "
            "dürfen verschieden sein; ohne lieferziel_id wird nur bei genau einer Adresse "
            "im Shopland abgeleitet. Bei waehrung != CHF tragen versand_chf, "
            "gratis_ab_chf und mindestbestellwert_chf die Originalbeträge; der Server "
            "rechnet sie mit belegtem Tageskurs um. Unbekannte Versandkosten werden als "
            "null erfasst, niemals als kostenlos."
        ),
        "record_offer": (
            "Angebot einer bekannten Zeile bei einem nicht gesperrten Shop erfassen "
            "oder die heutige Beobachtung aktualisieren; URL, Preis und wörtliche "
            "Liefer-/Lagertexte werden validiert. Die optionale shopinterne "
            "Artikelnummer ankert die Warenkorb-Prüfung sprachunabhängig; ohne sie "
            "zieht der Adapter sie beim ersten Füllen selbst von der Produktseite. "
            "Bei waehrung != CHF ist preis_chf der Preis in DIESER Währung; der Server "
            "rechnet selbst mit dem belegten Tageskurs in CHF um und legt Kurs, "
            "Kursdatum und Quelle dazu - niemals selbst umrechnen. Bei Marktplätzen "
            "nennt provenienz_text den sichtbaren Verkäufer und die Versandpartei "
            "wörtlich."
        ),
        "fetch_offer": (
            "Produktseite über den deklarativen Shop-Adapter deterministisch lesen "
            "und das Angebot mit wörtlichen Seitentexten erfassen. Respektiert "
            "robots.txt und einen Mindestabstand pro Domain; kein "
            "JavaScript-Rendering. Der Shop wird über die Domain der URL gefunden "
            "und muss bereits erfasst und nicht gesperrt sein. Geschrieben wird "
            "über denselben Pfad wie record_offer, mit derselben Kursumrechnung und "
            "demselben Lieferzeit-Parser; die Antwort führt zusätzlich den Rohtext "
            "je Feld. Ohne passenden Adapter kommt ein Klartextfehler - dann bleibt "
            "der manuelle Weg über record_offer mit wörtlich abgetippten Texten."
        ),
        "refresh_offer": (
            "Bestehendes Angebot über den Adapter neu von der Produktseite "
            "lesen; identifiziert über die offer_id, damit Zeile und URL nicht "
            "erneut zugeordnet werden müssen. Antwortet mit vorher/nachher und "
            "den wörtlichen Seitentexten. Tagesgenaue Historie: ein zweiter "
            "Aufruf am selben Tag überschreibt die heutige Beobachtung."
        ),
        "list_adapters": (
            "Geladene Shop-Adapter deterministisch nach id sortiert lesen: je "
            "Adapter id, Domain, abgedeckte Felder und Quelle (gebuendelt oder "
            "nutzer), dazu die Liste der übersprungenen Dateien mit Fehlergrund "
            "(read-only)."
        ),
        "mark_line": (
            "Zeile als Bestand, nichts gefunden oder erledigt markieren; bei Bestand "
            "wird die benötigte Menge aus dem Lager abgebucht."
        ),
        "plan_order": (
            "Bis zu drei Bestellvarianten aus den neuesten Angeboten nicht gesperrter "
            "Shops berechnen; Pins, Excludes, Versand, Mindestwerte, Lieferzeiten, "
            "Tempo, Unbekannt-Malus und der Abhol-Aufschlag je Nicht-Heim-Lieferziel "
            "werden berücksichtigt."
        ),
        "record_purchase": (
            "Tatsächlich ausgelöste vollständige Bestellung nach erneuter serverseitiger "
            "Planvalidierung samt zugesagten Liefertagen speichern und den Job auf "
            "bestellt setzen."
        ),
        "get_cart_session": (
            "Gast-Warenkorb beim Shop füllen und die Session übergeben: legt beim Shop "
            "eine Gast-Session an, füllt sie mit der persistierten Szenarioauswahl für "
            "diesen Shop und liest den Korb zurück; nur bei exakter Übereinstimmung von "
            "Artikelzahl und Zwischensumme wird übergeben, sonst kommt ein Fehler mit "
            "Diff. Kein Login, kein Kaufabschluss. Einziger DB-Schreibzugriff sind der "
            "Plattform-Befund und der Produkt-ID-Cache."
        ),
    }


def test_create_job_tool_forwards_lines_and_returns_parsed_confirmation():
    server = build_mcp(StubService())

    result = asyncio.run(
        server.call_tool("create_job", {"zeilen": ["2x Servo", "Kabel"]})
    )

    assert isinstance(result, tuple)
    assert result[1] == {
        "job_id": 41,
        "lines": [
            {"position": 1, "text": "Servo", "menge": 2},
            {"position": 2, "text": "Kabel", "menge": 1},
        ],
    }


def test_get_job_tool_returns_job_overview():
    result = asyncio.run(build_mcp(StubService()).call_tool("get_job", {"job_id": 9}))

    assert isinstance(result, tuple)
    assert result[1]["id"] == 9
    assert result[1]["scenarios_available"] is False


def test_delete_job_tool_requires_and_forwards_both_exact_ids():
    result = asyncio.run(
        build_mcp(StubService()).call_tool(
            "delete_job", {"job_id": 13, "confirm_job_id": 13}
        )
    )

    assert isinstance(result, tuple)
    assert result[1] == {"job_id": 13, "confirm_job_id": 13, "deleted": True}


def test_search_history_tool_returns_matching_purchases():
    result = asyncio.run(
        build_mcp(StubService()).call_tool("search_history", {"text": "Servo"})
    )

    assert isinstance(result, tuple)
    assert result[1] == {
        "result": [{"produktname": "Servo", "shop_name": "Swiss Shop"}]
    }


def test_get_stock_tool_returns_current_stock():
    result = asyncio.run(build_mcp(StubService()).call_tool("get_stock", {}))

    assert isinstance(result, tuple)
    assert result[1] == {"result": [{"bezeichnung": "Servo", "menge": 3}]}


def test_get_shops_tool_returns_ids_for_offer_recording():
    result = asyncio.run(build_mcp(StubService()).call_tool("get_shops", {}))

    assert isinstance(result, tuple)
    assert result[1] == {
        "result": [
            {
                "shop_id": 8,
                "name": "Reichelt",
                "url": "https://www.reichelt.de/",
                "lieferziel_id": 2,
            }
        ]
    }


def test_plan_scenarios_tool_forwards_all_optional_overrides():
    result = asyncio.run(
        build_mcp(StubService()).call_tool(
            "plan_scenarios",
            {"job_id": 9, "tempo": 0.7, "pins": {"10": 31}, "excludes": [32]},
        )
    )

    assert isinstance(result, tuple)
    assert result[1] == {
        "job_id": 9,
        "tempo": 0.7,
        "pins": {"10": 31},
        "excludes": [32],
        "ready": True,
        "scenarios": [],
    }


def test_get_cart_session_runs_the_same_adapter_path_as_the_ui():
    result = asyncio.run(
        build_mcp(StubService()).call_tool(
            "get_cart_session", {"job_id": 7, "shop_id": 1}
        )
    )

    assert isinstance(result, tuple)
    payload = result[1]
    assert payload["plattform"] == "opencart"
    assert payload["verifiziert"] is True
    assert payload["artikel_anzahl"] == 3
    assert payload["total_chf"] == "18.70"
    assert payload["cookie"] == {"name": "OCSESSID", "wert": "sess-1"}
    assert payload["positionen"] == [{"produktname": "Dupont", "menge": 2}]


def test_mcp_e2e_lists_the_cart_tool_but_never_calls_it():
    """Der Smoketest läuft gegen Produktion; get_cart_session würde dort einen
    echten Shop kontaktieren und schreiben. Listen ja, aufrufen nein."""
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert '"get_cart_session",' in source
    assert re.search(r'call\(\s*\d+\s*,\s*"get_cart_session"', source) is None


def test_mcp_e2e_lists_delete_job_but_never_calls_it():
    """Der Produktions-Smoketest darf niemals einen echten Job löschen."""
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert '"delete_job",' in source
    assert re.search(r'call\(\s*\d+\s*,\s*"delete_job"', source) is None


def test_mcp_e2e_invokes_create_job_only_with_non_writing_invalid_input():
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert 'call(3, "create_job", {"zeilen": []})' in source
    assert '"create_job_write": False' in source
    assert '"zeilen": ["' not in source


def test_mcp_e2e_calls_get_shops_and_checks_the_public_shop_id():
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert 'call(7, "get_shops", {})' in source
    assert '"shop_id"' in source


def test_mcp_e2e_lists_fetch_offer_but_never_calls_it():
    """Der Smoketest läuft gegen Produktion; fetch_offer würde dort einen echten
    Shop abrufen und ein Angebot schreiben. Listen ja, aufrufen nein."""
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert '"fetch_offer",' in source
    assert re.search(r'call\(\s*\d+\s*,\s*"fetch_offer"', source) is None


def test_mcp_e2e_calls_list_adapters_because_it_reads_only():
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert 'call(8, "list_adapters", {})' in source
    assert '"adapter_count"' in source


def test_streamable_http_endpoint_initializes_and_lists_tools():
    class Repository:
        def create_job(self, *_):
            return 1

        def get_job(self, *_):
            return None

    app = create_app(Repository(), lambda: 1)
    headers = {"Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    with TestClient(app) as client:
        initialized = client.post("/mcp", headers=headers, json=initialize)
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )

    assert initialized.status_code == 200
    assert initialized.json()["result"]["serverInfo"]["name"] == "beschaffung"
    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert names == {
        "get_cart_session",
        "create_job",
        "delete_job",
        "get_job",
        "search_history",
        "get_stock",
        "get_shops",
        "plan_scenarios",
        "next_job",
        "check_line",
        "check_stock",
        "adjust_stock",
        "record_shop",
        "record_offer",
        "fetch_offer",
        "refresh_offer",
        "list_adapters",
        "mark_line",
        "plan_order",
        "record_purchase",
    }


def test_record_shop_accepts_an_explicit_delivery_target_and_stays_compatible():
    tools = asyncio.run(build_mcp(StubService()).list_tools())
    record_shop = next(tool for tool in tools if tool.name == "record_shop")
    properties = record_shop.inputSchema["properties"]
    pflicht = set(record_shop.inputSchema.get("required", []))

    # Additiv: neu und optional, die bestehende Signatur bleibt aufrufbar.
    assert "lieferziel_id" in properties
    assert "lieferziel_id" not in pflicht
    assert {
        "name",
        "url",
        "land",
        "versand_chf",
        "profil_quelle_url",
        "versand_text",
    } <= pflicht


def test_record_offer_exposes_currency_and_article_number_as_optional():
    tools = asyncio.run(build_mcp(StubService()).list_tools())
    record_offer = next(tool for tool in tools if tool.name == "record_offer")
    properties = record_offer.inputSchema["properties"]
    pflicht = set(record_offer.inputSchema.get("required", []))

    assert "waehrung" in properties and "waehrung" not in pflicht
    assert "artikelnummer" in properties and "artikelnummer" not in pflicht
    assert "provenienz_text" in properties and "provenienz_text" not in pflicht


def test_the_engine_reads_the_page_and_the_ai_cannot_pose_as_an_adapter():
    tools = asyncio.run(build_mcp(StubService()).list_tools())
    fetch_offer = next(tool for tool in tools if tool.name == "fetch_offer")
    record_offer = next(tool for tool in tools if tool.name == "record_offer")
    list_adapters = next(tool for tool in tools if tool.name == "list_adapters")

    # Die KI nennt Zeile und URL, die Engine liest den Rest von der Seite.
    assert set(fetch_offer.inputSchema["properties"]) == {"line_id", "produkt_url"}
    assert set(fetch_offer.inputSchema.get("required", [])) == {
        "line_id",
        "produkt_url",
    }
    # Den Erfassungsweg setzt ausschliesslich die Engine.
    assert "erfasst_via" not in record_offer.inputSchema["properties"]
    assert list_adapters.inputSchema["properties"] == {}


def test_the_descriptions_state_the_currency_and_target_rules():
    tools = asyncio.run(build_mcp(StubService()).list_tools())
    beschreibung = {tool.name: tool.description for tool in tools}

    # Waehrung: der Server rechnet, der Agent liefert den Originalpreis.
    assert "niemals selbst umrechnen" in beschreibung["record_offer"]
    assert "Tageskurs" in beschreibung["record_offer"]
    assert "Verkäufer" in beschreibung["record_offer"]
    # Shopherkunft, Lieferziel und Versandwährung sind getrennte Fakten.
    assert (
        "Shopland und Lieferziel dürfen verschieden sein" in beschreibung["record_shop"]
    )
    assert "ohne lieferziel_id" in beschreibung["record_shop"]
    assert "Tageskurs" in beschreibung["record_shop"]
    assert "null erfasst, niemals als kostenlos" in beschreibung["record_shop"]
    # Aufschlag und Indikator stehen im Vertrag, nicht nur im Code.
    assert "einmal pro Ziel und Plan" in beschreibung["plan_scenarios"]
    assert "keine Steuer berechnet" in beschreibung["plan_scenarios"]
    assert "Abhol-Aufschlag" in beschreibung["plan_order"]
