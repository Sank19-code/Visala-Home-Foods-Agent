# The MCP layer itself: the right tools are registered, and none of them can approve a purchase.
import asyncio
import json

from src.commerce_mcp.server import mcp

EXPECTED = {
    "search_products", "get_product", "create_cart", "add_to_cart", "update_cart_item",
    "get_cart", "check_delivery", "get_checkout_quote", "create_order", "get_order_status",
}


def _tools():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_expected_tools_are_registered():
    assert set(_tools()) == EXPECTED


def test_agent_has_no_way_to_confirm_its_own_purchase():
    assert not any("approve" in n or "confirm" in n for n in _tools())


def test_every_tool_has_a_description():
    assert all(t.description and len(t.description) > 20 for t in _tools().values())


def test_tool_call_through_mcp(store):
    result = asyncio.run(mcp.call_tool("search_products", {"category": "podi"}))
    content = result[0] if isinstance(result, tuple) else result
    structured = json.loads(content[0].text)  # tools return JSON text content
    assert structured["ok"] and {p["category"] for p in structured["products"]} == {"podi"}
