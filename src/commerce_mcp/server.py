# MCP server exposing the Visala Home Foods storefront as agent-callable tools.
#
#   python -m src.commerce_mcp.server            stdio (Claude Desktop, the buyer agent)
#   python -m src.commerce_mcp.server --http     streamable HTTP on :8001/mcp
#
# Tools (10):
#   catalogue  search_products, get_product
#   cart       create_cart, add_to_cart, update_cart_item, get_cart
#   delivery   check_delivery
#   checkout   get_checkout_quote, create_order, get_order_status
#
# Every call goes through runtime.run_tool, which writes the audit log. Guardrails live in the
# tool code, not in these descriptions: the descriptions tell the agent the rules, the code
# enforces them whether or not the agent listens.
import argparse
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from src.commerce_mcp.runtime import run_tool
from src.commerce_mcp.tools import cart, catalog, delivery, orders

INSTRUCTIONS = """\
You are shopping at Visala Home Foods (homemade pickles, podis and snacks) for a human user.
Rules the store enforces:
- Prices are integer paise (₹1 = 100 paise). Use the *_display fields when talking to the user.
- Never invent products or prices; only use what the tools return.
- If an item is out of stock, tell the user and offer a substitute only after asking.
- Before create_order, call get_checkout_quote, show the user its `summary` and get their
  approval. Their approval produces the confirmation_token; you cannot create one.
- Use one idempotency_key per purchase attempt and reuse it on retries.
- You never pay. create_order returns a payment link for the user to pay themselves.
"""

mcp = FastMCP("visala-commerce", instructions=INSTRUCTIONS)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


# --- catalogue -------------------------------------------------------------------------------


@mcp.tool(annotations=READ)
def search_products(
    query: Annotated[str | None, Field(description="Free text, e.g. 'mango pickle' or 'spicy podi'")] = None,
    category: Annotated[str | None, Field(description="pickle | podi | snack")] = None,
    max_price_inr: Annotated[int | None, Field(ge=0, description="Max unit price in whole rupees")] = None,
    spice_level: Annotated[str | None, Field(description="mild | medium | hot")] = None,
    in_stock_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=20)] = 10,
) -> dict:
    """Search the catalogue. Returns price, stock, weight, spice level and shelf life.
    Out-of-stock items are included (in_stock=false) unless in_stock_only is true."""
    return run_tool(
        "search_products", catalog.search_products,
        query=query, category=category, max_price_inr=max_price_inr,
        spice_level=spice_level, in_stock_only=in_stock_only, limit=limit,
    )


@mcp.tool(annotations=READ)
def get_product(
    product_id: Annotated[str, Field(description="e.g. PKL-MNG-250")],
) -> dict:
    """Full details for one product. If it is out of stock, includes in-stock substitutes
    from the same category — ask the user before using one."""
    return run_tool("get_product", catalog.get_product, product_id=product_id)


# --- cart ------------------------------------------------------------------------------------


@mcp.tool(annotations=WRITE)
def create_cart(
    customer_ref: Annotated[str, Field(description="Stable id for the user, e.g. 'user_42'")],
) -> dict:
    """Start a new empty cart for this user. Use one cart per purchase."""
    return run_tool("create_cart", cart.create_cart, customer_ref=customer_ref)


@mcp.tool(annotations=WRITE)
def add_to_cart(
    cart_id: str,
    product_id: str,
    qty: Annotated[int, Field(ge=1, le=10)] = 1,
) -> dict:
    """Add qty of a product (adds to any existing qty). Returns the cart with a running
    items total. Fails with out_of_stock (plus substitutes) if there is not enough stock."""
    return run_tool("add_to_cart", cart.add_to_cart, cart_id=cart_id, product_id=product_id, qty=qty)


@mcp.tool(annotations=WRITE)
def update_cart_item(
    cart_id: str,
    product_id: str,
    qty: Annotated[int, Field(ge=0, le=10, description="New quantity; 0 removes the line")],
) -> dict:
    """Set the exact quantity of a product in the cart (0 removes it)."""
    return run_tool(
        "update_cart_item", cart.update_cart_item, cart_id=cart_id, product_id=product_id, qty=qty
    )


@mcp.tool(annotations=READ)
def get_cart(cart_id: str) -> dict:
    """Current cart lines and items total (delivery not included)."""
    return run_tool("get_cart", cart.get_cart, cart_id=cart_id)


# --- delivery --------------------------------------------------------------------------------


def _check_delivery(_session, **kwargs) -> dict:
    return delivery.check_delivery(**kwargs)


@mcp.tool(annotations=READ)
def check_delivery(
    pincode: Annotated[str, Field(description="6-digit Indian pincode")],
    requested_date: Annotated[str | None, Field(description="YYYY-MM-DD, optional")] = None,
    items_total_inr: Annotated[int | None, Field(ge=0, description="To compute the charge")] = None,
) -> dict:
    """Is this pincode serviceable, what does delivery cost, and the earliest delivery date.
    If requested_date is earlier than possible, says so — ask the user before accepting."""
    return run_tool(
        "check_delivery", _check_delivery,
        pincode=pincode, requested_date=requested_date, items_total_inr=items_total_inr,
    )


# --- checkout --------------------------------------------------------------------------------


@mcp.tool(annotations=READ)
def get_checkout_quote(
    cart_id: str,
    pincode: Annotated[str, Field(description="Delivery pincode")],
) -> dict:
    """Exact payable total from LIVE prices and stock, including delivery, plus the spend
    limits. Show `summary` to the user word for word and ask them to approve it."""
    return run_tool("get_checkout_quote", orders.get_checkout_quote, cart_id=cart_id, pincode=pincode)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                      idempotentHint=True, openWorldHint=True))
def create_order(
    cart_id: str,
    customer_name: str,
    customer_phone: Annotated[str, Field(description="10-digit Indian mobile number")],
    pincode: str,
    idempotency_key: Annotated[str, Field(
        min_length=8, max_length=80,
        description="One per purchase attempt (e.g. a UUID). Reuse the SAME key on retries.")],
    confirmation_token: Annotated[str | None, Field(
        description="Issued when the user approves the quote. You cannot create this yourself.")] = None,
) -> dict:
    """Place the order and get a Razorpay payment link for the USER to pay.
    Re-checks live price and stock, enforces spend limits, and requires the user's
    confirmation for this exact cart and total. Safe to retry with the same idempotency_key:
    you get the original order back, never a duplicate."""
    return run_tool(
        "create_order", orders.create_order,
        cart_id=cart_id, customer_name=customer_name, customer_phone=customer_phone,
        pincode=pincode, idempotency_key=idempotency_key, confirmation_token=confirmation_token,
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def get_order_status(order_id: str) -> dict:
    """Order status: pending | paid | failed | expired | cancelled. Poll this after the user
    has been given the payment link."""
    return run_tool("get_order_status", orders.get_order_status, order_id=order_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Visala commerce MCP server")
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    # Fail fast (before any agent connects) on live keys or a missing .env.
    from src.config import settings  # noqa: F401  (import runs validate_test_mode)

    if args.http:
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
