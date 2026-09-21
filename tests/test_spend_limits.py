# Order above MAX_ORDER_AMOUNT_INR is blocked; daily cap accumulates per user.
import pytest

from src.commerce_mcp.tools.orders import approve_quote
from src.errors import SpendLimitExceeded
from src.guardrails import spend_limits
from tests.helpers import cart_with, place


def test_per_order_cap_blocks_large_orders():
    spend_limits.check_order_amount(200000, max_order_paise=200000)  # exactly at the cap: ok
    with pytest.raises(SpendLimitExceeded):
        spend_limits.check_order_amount(200001, max_order_paise=200000)


def test_order_above_cap_is_blocked_end_to_end(store):
    factory, gateway = store
    # 10 mango (₹1,800) + 2 lemon (₹340) = ₹2,140 > ₹2,000 default cap
    cart_id = cart_with(("PKL-MNG-250", 10), ("PKL-LMN-250", 2))
    res = place(factory, cart_id, "idem-bigorder-01")
    assert res["error"]["code"] == "spend_limit_exceeded"
    assert res["error"]["details"]["limit"] == "per_order"
    assert gateway.calls == []


def test_daily_cap_accumulates_per_customer(store):
    factory, _ = store
    # Default daily cap ₹5,000. Three ₹1,800 orders: 1,800 + 1,800 = 3,600 ok; 5,400 blocked.
    results = []
    for i in range(3):
        cart_id = cart_with(("PKL-MNG-250", 10), customer_ref="user_daily")
        results.append(place(factory, cart_id, f"idem-daily-{i:04d}"))
    assert results[0]["ok"] and results[1]["ok"]
    assert results[2]["error"]["code"] == "spend_limit_exceeded"
    assert results[2]["error"]["details"]["limit"] == "per_day"

    # A different customer is unaffected.
    other = cart_with(("PKL-MNG-250", 1), customer_ref="user_other")
    assert place(factory, other, "idem-daily-other")["ok"]


def test_quote_reports_headroom(store):
    factory, _ = store
    from src.commerce_mcp import server

    cart_id = cart_with(("PKL-MNG-250", 10), ("PKL-LMN-250", 2))
    quote = server.get_checkout_quote(cart_id, "560001")
    assert quote["ok"] and quote["within_spend_limits"] is False
    with factory() as s:  # the human can still be shown it, but create_order will refuse
        assert approve_quote(s, cart_id, "560001")["amount_paise"] == 214000
