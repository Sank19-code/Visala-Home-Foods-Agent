# Small helpers shared by the commerce tool tests.
from src.commerce_mcp import server
from src.commerce_mcp.tools.orders import approve_quote

CUSTOMER = {"customer_name": "Test Buyer", "customer_phone": "9876543210"}
PINCODE = "560001"


def cart_with(*lines: tuple[str, int], customer_ref: str = "user_1") -> str:
    cart_id = server.create_cart(customer_ref)["cart_id"]
    for product_id, qty in lines:
        res = server.add_to_cart(cart_id, product_id, qty)
        assert res["ok"], res
    return cart_id


def approve(factory, cart_id: str, pincode: str = PINCODE) -> str:
    # What the human-side CLI does after the user says "yes".
    with factory() as s:
        return approve_quote(s, cart_id, pincode)["confirmation_token"]


def place(factory, cart_id: str, key: str, pincode: str = PINCODE, token: str | None = "auto"):
    if token == "auto":
        token = approve(factory, cart_id, pincode)
    return server.create_order(
        cart_id=cart_id, pincode=pincode, idempotency_key=key, confirmation_token=token, **CUSTOMER
    )
