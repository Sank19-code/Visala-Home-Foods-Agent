# End-to-end with the Razorpay client faked: cart -> create_order -> webhook -> status paid.
# (The webhook half lands with src/payments/webhooks.py; until then the paid transition is
# driven through get_order_status polling, which uses the same apply_payment_status.)
import json

from src.commerce_mcp import server
from src.db.models import AuditEvent, Cart, Product
from tests.helpers import PINCODE, approve, cart_with, place


def test_happy_path(store):
    factory, gateway = store
    found = server.search_products(query="mango pickles")
    assert found["ok"] and found["products"][0]["product_id"] == "PKL-MNG-250"

    cart_id = cart_with(("PKL-MNG-250", 2), ("POD-GUN-200", 1))
    quote = server.get_checkout_quote(cart_id, PINCODE)
    # 2 x 180 + 150 = 510, below free-delivery threshold -> +49 = 559
    assert quote["amount_paise"] == 55900 and quote["delivery_paise"] == 4900
    assert quote["amount_paise"] <= 60000  # the eval's "under 600 rupees"

    order = place(factory, cart_id, "idem-happy-0001")
    assert order["ok"] and order["status"] == "pending"
    assert order["payment_link_url"].startswith("https://rzp.io/")
    assert gateway.calls[0]["amount_paise"] == 55900
    assert gateway.calls[0]["reference_id"] == order["order_id"]

    with factory() as s:
        assert s.get(Product, "PKL-MNG-250").stock == 38  # reserved at order time
        assert s.get(Cart, cart_id).status == "checked_out"

    link_id = next(iter(gateway.links))
    gateway.mark(link_id, "paid")
    status = server.get_order_status(order["order_id"])
    assert status["status"] == "paid" and status["paid_at"]


def test_order_without_confirmation_is_blocked(store):
    factory, gateway = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    res = place(factory, cart_id, "idem-noconf-001", token=None)
    assert res["error"]["code"] == "confirmation_required"
    assert gateway.calls == []


def test_forged_confirmation_token_is_rejected(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    token = approve(factory, cart_id)
    payload, _sig = token.split(".")
    res = place(factory, cart_id, "idem-forged-001", token=f"{payload}.AAAA")
    assert res["error"]["code"] == "confirmation_required"


def test_changing_the_cart_invalidates_the_confirmation(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    token = approve(factory, cart_id)
    server.add_to_cart(cart_id, "SNK-MUR-250", 1)  # agent sneaks in one more
    res = place(factory, cart_id, "idem-changed-01", token=token)
    assert res["error"]["code"] == "confirmation_required"


def test_confirmation_for_another_pincode_is_rejected(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    token = approve(factory, cart_id, "560001")
    res = place(factory, cart_id, "idem-pincode-01", pincode="600001", token=token)
    assert res["error"]["code"] == "confirmation_required"


def test_price_change_after_quote_is_caught(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    token = approve(factory, cart_id)
    with factory() as s:
        s.get(Product, "SNK-MUR-250").price_paise = 14000
        s.commit()
    res = place(factory, cart_id, "idem-price-0001", token=token)
    assert res["error"]["code"] == "price_changed"
    # After re-quoting and a fresh approval, the order goes through at the new price.
    res = place(factory, cart_id, "idem-price-0002")
    assert res["ok"] and res["items_total_paise"] == 14000


def test_out_of_stock_offers_substitutes(store):
    res = server.add_to_cart(server.create_cart("user_1")["cart_id"], "POD-CUR-200", 2)
    assert res["error"]["code"] == "out_of_stock"
    subs = res["error"]["details"]["substitutes"]
    assert subs and all(s["category"] == "podi" and s["in_stock"] for s in subs)


def test_unserviceable_pincode(store):
    cart_id = cart_with(("SNK-MUR-250", 1))
    res = server.get_checkout_quote(cart_id, "110001")
    assert res["error"]["code"] == "not_serviceable"
    assert server.check_delivery("110001")["serviceable"] is False


def test_free_delivery_threshold(store):
    cart_id = cart_with(("PKL-MNG-250", 5))  # ₹900 >= ₹799
    assert server.get_checkout_quote(cart_id, PINCODE)["delivery_paise"] == 0


def test_expired_link_releases_stock(store):
    factory, gateway = store
    cart_id = cart_with(("SNK-THT-250", 2))
    order = place(factory, cart_id, "idem-expire-001")
    gateway.mark(next(iter(gateway.links)), "expired")
    assert server.get_order_status(order["order_id"])["status"] == "expired"
    with factory() as s:
        assert s.get(Product, "SNK-THT-250").stock == 12


def test_every_tool_call_is_audited_with_secrets_redacted(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    place(factory, cart_id, "idem-audit-0001")
    server.get_product("NOPE")
    with factory() as s:
        events = s.query(AuditEvent).order_by(AuditEvent.id).all()
    actions = [e.action for e in events]
    assert actions == ["create_cart", "add_to_cart", "create_order", "get_product"]
    order_event = events[2]
    args = json.loads(order_event.args_json)
    assert args["confirmation_token"] == "***"
    assert args["customer_phone"].endswith("3210") and "9876" not in args["customer_phone"]
    assert events[3].outcome == "error"


def test_blocked_calls_are_audited_as_blocked(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    place(factory, cart_id, "idem-blocked-01", token=None)
    with factory() as s:
        last = s.query(AuditEvent).order_by(AuditEvent.id.desc()).first()
    assert last.action == "create_order" and last.outcome == "blocked"


def test_unknown_ids_give_structured_errors(store):
    assert server.get_cart("cart_nope")["error"]["code"] == "not_found"
    assert server.get_order_status("ord_nope")["error"]["code"] == "not_found"


def test_search_understands_everyday_words(store):
    hot = server.search_products(query="something spicy")["products"]
    assert hot and all(p["spice_level"] == "hot" for p in hot)
