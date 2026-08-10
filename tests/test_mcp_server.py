import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.mcp_server import build_mcp
from app.web import create_app


class StubService:
    def create_job_from_lines(self, lines):
        return {
            "job_id": 41,
            "lines": [
                {"position": index, "text": line.removeprefix("2x "), "menge": 2 if line.startswith("2x ") else 1}
                for index, line in enumerate(lines, 1)
            ],
        }

    def get_job(self, job_id):
        return {"id": job_id, "status": "in_arbeit", "lines": [], "scenarios_available": False}

    def search_history(self, text):
        return [{"produktname": text, "shop_name": "Swiss Shop"}]

    def get_stock(self):
        return [{"bezeichnung": "Servo", "menge": 3}]

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

    def record_shop(self, *args):
        return {"id": 1}

    def record_offer(self, *args):
        return {"id": 2}

    def mark_line(self, *args):
        return {"id": args[0]}

    def plan_order(self, *args):
        return []

    def record_purchase(self, *args):
        return {"id": 3}


def test_mcp_exposes_exact_procurement_tools():
    server = build_mcp(StubService())

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "create_job",
        "get_job",
        "search_history",
        "get_stock",
        "plan_scenarios",
        "next_job",
        "check_line",
        "record_shop",
        "record_offer",
        "mark_line",
        "plan_order",
        "record_purchase",
    }
    record_offer = next(tool for tool in tools if tool.name == "record_offer")
    properties = record_offer.inputSchema["properties"]
    assert "lieferzeit_text" in properties
    assert "lager_text" in properties
    assert "lieferzeit_tage" not in properties
    assert "lager" not in properties


def test_all_tool_descriptions_match_current_behavior():
    tools = asyncio.run(build_mcp(StubService()).list_tools())
    descriptions = {tool.name: tool.description for tool in tools}

    assert descriptions == {
        "create_job": (
            "Echten UI-Job aus einer Zeilenliste anlegen; Mengenpräfixe wie «2x …» "
            "werden über denselben Parser wie die Textarea verarbeitet."
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
        "plan_scenarios": (
            "Die vollständige Szenariomatrix mit optionalem Tempo, Pins und Excludes "
            "über exakt dieselbe Serverfunktion wie die Job-UI berechnen (read-only)."
        ),
        "next_job": (
            "Ältesten nicht als Test markierten offenen Job mit seinen noch offenen "
            "oder in Arbeit befindlichen Zeilen laden."
        ),
        "check_line": (
            "Exakten Bestand, frühere Käufe und höchstens 14 Tage alte Angebote für "
            "eine Zeile gemeinsam laden (read-only)."
        ),
        "record_shop": (
            "Neuen Schweizer Shop mit angegebener HTTP(S)-Profilquelle, "
            "Versand-Originaltext und validierten Profilwerten erfassen."
        ),
        "record_offer": (
            "Angebot einer bekannten Zeile bei einem nicht gesperrten Shop erfassen "
            "oder die heutige Beobachtung aktualisieren; URL, Preis und wörtliche "
            "Liefer-/Lagertexte werden validiert."
        ),
        "mark_line": (
            "Zeile als Bestand, nichts gefunden oder erledigt markieren; bei Bestand "
            "wird die benötigte Menge aus dem Lager abgebucht."
        ),
        "plan_order": (
            "Bis zu drei Bestellvarianten aus den neuesten Angeboten nicht gesperrter "
            "Shops berechnen; Pins, Excludes, Versand, Mindestwerte, Lieferzeiten, "
            "Tempo und Unbekannt-Malus werden berücksichtigt."
        ),
        "record_purchase": (
            "Tatsächlich ausgelöste vollständige Bestellung nach erneuter serverseitiger "
            "Planvalidierung samt zugesagten Liefertagen speichern und den Job auf "
            "bestellt setzen."
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


def test_search_history_tool_returns_matching_purchases():
    result = asyncio.run(
        build_mcp(StubService()).call_tool("search_history", {"text": "Servo"})
    )

    assert isinstance(result, tuple)
    assert result[1] == {"result": [{"produktname": "Servo", "shop_name": "Swiss Shop"}]}


def test_get_stock_tool_returns_current_stock():
    result = asyncio.run(build_mcp(StubService()).call_tool("get_stock", {}))

    assert isinstance(result, tuple)
    assert result[1] == {"result": [{"bezeichnung": "Servo", "menge": 3}]}


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


def test_mcp_e2e_invokes_create_job_only_with_non_writing_invalid_input():
    source = Path("tests/e2e/mcp_tools_call.py").read_text(encoding="utf-8")

    assert 'call(3, "create_job", {"zeilen": []})' in source
    assert '"create_job_write": False' in source
    assert '"zeilen": ["' not in source


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
        "create_job",
        "get_job",
        "search_history",
        "get_stock",
        "plan_scenarios",
        "next_job",
        "check_line",
        "record_shop",
        "record_offer",
        "mark_line",
        "plan_order",
        "record_purchase",
    }
