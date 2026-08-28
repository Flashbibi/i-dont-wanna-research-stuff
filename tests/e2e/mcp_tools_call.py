#!/usr/bin/env python3
"""Read-only production MCP smoke test.

create_job is invoked only with invalid empty input, so this E2E never creates a real job.

get_cart_session is listed but never called: it opens a guest session at a real
shop and writes the platform finding plus the product-id cache. Keep it out of
tools/call here.

adjust_stock is listed but never called: it writes stock. Keep it
out of tools/call here. check_stock is read-only but covered by
the unit suite; the smoke test only asserts its presence.

fetch_offer and refresh_offer are listed but never called: they reach a real
shop over the network and write an offer. Keep them out of tools/call here.
list_adapters is read-only and touches neither, so it is called.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("BASE_URL", "http://192.168.1.60:8000").rstrip("/")
MCP_URL = f"{BASE_URL}/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
EXPECTED_TOOLS = {
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


def rpc(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    request = Request(
        MCP_URL,
        data=json.dumps(payload).encode(),
        headers=HEADERS,
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise AssertionError(f"{method}: HTTP {response.status}")
        return json.load(response)


def call(request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = rpc(
        request_id,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    return body["result"]


def main() -> None:
    initialized = rpc(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "beschaffung-mcp-e2e", "version": "1"},
        },
    )
    assert initialized["result"]["serverInfo"]["name"] == "beschaffung"

    listed = rpc(2, "tools/list", {})
    tools = listed["result"]["tools"]
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS
    assert all(tool.get("description") for tool in tools)

    invalid_create = call(3, "create_job", {"zeilen": []})
    assert invalid_create["isError"] is True
    assert "mindestens eine Position" in invalid_create["content"][0]["text"]

    job = call(4, "get_job", {"job_id": 1})
    assert job["isError"] is False
    assert job["structuredContent"]["id"] == 1
    assert "scenarios_available" in job["structuredContent"]
    assert all("candidate_count" in line for line in job["structuredContent"]["lines"])

    history = call(5, "search_history", {"text": "__mcp_e2e_no_match__"})
    assert history["isError"] is False

    stock = call(6, "get_stock", {})
    assert stock["isError"] is False

    shops = call(7, "get_shops", {})
    assert shops["isError"] is False
    shop_rows = shops["structuredContent"]["result"]
    assert shop_rows
    assert all("shop_id" in shop and "name" in shop for shop in shop_rows)

    adapters = call(8, "list_adapters", {})
    assert adapters["isError"] is False
    adapter_state = adapters["structuredContent"]
    assert "adapter" in adapter_state and "fehler" in adapter_state
    assert [eintrag["id"] for eintrag in adapter_state["adapter"]] == sorted(
        eintrag["id"] for eintrag in adapter_state["adapter"]
    )

    scenarios = call(
        9,
        "plan_scenarios",
        {"job_id": 1, "tempo": 0.5, "pins": {}, "excludes": []},
    )
    assert scenarios["isError"] is False
    matrix = scenarios["structuredContent"]
    assert matrix["job_id"] == 1
    assert all(
        key in matrix
        for key in ("lines", "open_lines", "scenarios", "custom", "pins", "excludes")
    )

    print(
        json.dumps(
            {
                "ok": True,
                "tool_count": len(tools),
                "shop_count": len(shop_rows),
                "job_id": job["structuredContent"]["id"],
                "scenario_count": len(matrix["scenarios"]),
                "adapter_count": len(adapter_state["adapter"]),
                "adapter_errors": len(adapter_state["fehler"]),
                "create_job_write": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
