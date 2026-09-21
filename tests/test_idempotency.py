# Same idempotency key -> one order, and the second call returns the first order's id.
from src.commerce_mcp import server
from src.db.models import Order
from tests.helpers import CUSTOMER, PINCODE, cart_with, place


def test_replay_returns_the_original_order(store):
    factory, gateway = store
    cart_id = cart_with(("SNK-MUR-250", 1))

    first = place(factory, cart_id, "idem-replay-001")
    assert first["ok"], first
    token_again = None  # a replay does not even need a fresh confirmation
    replays = [
        server.create_order(cart_id=cart_id, pincode=PINCODE, idempotency_key="idem-replay-001",
                            confirmation_token=token_again, **CUSTOMER)
        for _ in range(3)
    ]

    assert all(r["ok"] and r["replayed"] for r in replays)
    assert {r["order_id"] for r in replays} == {first["order_id"]}
    with factory() as s:
        assert s.query(Order).count() == 1
    assert len(gateway.calls) == 1  # exactly one payment link


def test_same_key_for_a_different_request_is_refused(store):
    factory, _ = store
    cart_a = cart_with(("SNK-MUR-250", 1))
    cart_b = cart_with(("SNK-THT-250", 1))
    assert place(factory, cart_a, "idem-shared-001")["ok"]

    res = place(factory, cart_b, "idem-shared-001")
    assert not res["ok"]
    assert res["error"]["code"] == "idempotency_conflict"


def test_new_key_on_an_already_ordered_cart_is_a_conflict(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    assert place(factory, cart_id, "idem-first-0001")["ok"]
    res = place(factory, cart_id, "idem-second-001", token=None)
    assert res["error"]["code"] == "conflict"


def test_payment_link_failure_is_recovered_by_retrying_the_same_key(store):
    factory, gateway = store
    cart_id = cart_with(("SNK-MUR-250", 2))
    gateway.fail_next = True

    first = place(factory, cart_id, "idem-retry-0001")
    assert first["error"]["code"] == "payment_link_failed"

    retry = place(factory, cart_id, "idem-retry-0001", token=None)
    assert retry["ok"] and retry["replayed"] and retry["payment_link_url"]
    assert retry["order_id"] == first["error"]["details"]["order_id"]
    with factory() as s:
        assert s.query(Order).count() == 1


def test_bad_key_format_is_rejected(store):
    factory, _ = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    res = place(factory, cart_id, "short")
    assert res["error"]["code"] == "invalid_input"
