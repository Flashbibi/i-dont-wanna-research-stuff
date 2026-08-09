import asyncio

from fastapi.testclient import TestClient

from app.mcp_server import build_mcp
from app.web import create_app


class StubService:
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
        "next_job",
        "check_line",
        "record_shop",
        "record_offer",
        "mark_line",
        "plan_order",
        "record_purchase",
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
        "next_job",
        "check_line",
        "record_shop",
        "record_offer",
        "mark_line",
        "plan_order",
        "record_purchase",
    }
