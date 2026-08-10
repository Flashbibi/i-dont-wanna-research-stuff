import asyncio

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
