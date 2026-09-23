# The human approval page: the fix for "Claude Desktop has no way to approve" (test_reviews/test1).
import hashlib
import hmac
import re

from fastapi.testclient import TestClient

from src.commerce_mcp import server
from src import config
from src.db.models import Order
from src.payments.webhooks import app
from tests.helpers import CUSTOMER, PINCODE, cart_with


def _approve_in_browser(client, cart_id, decision="approve"):
    page = client.get(f"/approve/{cart_id}", params={"pincode": PINCODE})
    assert page.status_code == 200
    fp = re.search(r'name="fingerprint" value="([0-9a-f]+)"', page.text).group(1)
    return client.post(f"/approve/{cart_id}",
                       data={"pincode": PINCODE, "fingerprint": fp, "decision": decision})


def _order_without_token(cart_id, key):
    return server.create_order(cart_id=cart_id, pincode=PINCODE, idempotency_key=key, **CUSTOMER)


def test_quote_points_to_the_approval_page(store):
    cart_id = cart_with(("PKL-MNG-250", 2))
    quote = server.get_checkout_quote(cart_id, PINCODE)
    assert quote["approval_url"].endswith(f"/approve/{cart_id}?pincode={PINCODE}")


def test_page_shows_the_server_computed_total(store):
    cart_id = cart_with(("PKL-MNG-250", 2))
    page = TestClient(app).get(f"/approve/{cart_id}", params={"pincode": PINCODE})
    assert "₹409" in page.text and "Mango Pickle" in page.text


def test_approving_in_the_browser_lets_create_order_through(store):
    cart_id = cart_with(("PKL-MNG-250", 2))
    blocked = _order_without_token(cart_id, "idem-page-0001")
    assert blocked["error"]["code"] == "confirmation_required"
    assert blocked["error"]["details"]["approval_url"]

    assert "Approved" in _approve_in_browser(TestClient(app), cart_id).text
    placed = _order_without_token(cart_id, "idem-page-0002")
    assert placed["ok"] and placed["amount_paise"] == 40900


def test_declining_does_not_approve(store):
    cart_id = cart_with(("PKL-MNG-250", 1))
    assert "Declined" in _approve_in_browser(TestClient(app), cart_id, "decline").text
    assert _order_without_token(cart_id, "idem-page-0003")["error"]["code"] == "confirmation_required"


def test_changing_the_cart_after_approval_voids_it(store):
    cart_id = cart_with(("PKL-MNG-250", 1))
    _approve_in_browser(TestClient(app), cart_id)
    server.add_to_cart(cart_id, "PKL-MNG-250", 1)
    assert _order_without_token(cart_id, "idem-page-0004")["error"]["code"] == "confirmation_required"


def test_stale_page_cannot_approve_a_changed_cart(store):
    client = TestClient(app)
    cart_id = cart_with(("PKL-MNG-250", 1))
    page = client.get(f"/approve/{cart_id}", params={"pincode": PINCODE})
    fp = re.search(r'name="fingerprint" value="([0-9a-f]+)"', page.text).group(1)
    server.add_to_cart(cart_id, "PKL-MNG-250", 1)
    resp = client.post(f"/approve/{cart_id}",
                       data={"pincode": PINCODE, "fingerprint": fp, "decision": "approve"})
    assert resp.status_code == 409


def test_over_limit_cart_cannot_be_approved(store):
    cart_id = cart_with(("PKL-MNG-250", 10), ("PKL-LMN-250", 2))
    page = TestClient(app).get(f"/approve/{cart_id}", params={"pincode": PINCODE})
    assert "cannot be approved" in page.text and 'name="fingerprint"' not in page.text


def test_signed_payment_callback_marks_paid(store):
    factory, gateway = store
    cart_id = cart_with(("SNK-MUR-250", 1))
    client = TestClient(app)
    _approve_in_browser(client, cart_id)
    order = _order_without_token(cart_id, "idem-callback-1")
    link_id = next(iter(gateway.links))
    params = {
        "razorpay_payment_id": "pay_abc",
        "razorpay_payment_link_id": link_id,
        "razorpay_payment_link_reference_id": order["order_id"],
        "razorpay_payment_link_status": "paid",
    }
    msg = "|".join([link_id, order["order_id"], "paid", "pay_abc"]).encode()
    good = hmac.new(config.settings.razorpay_key_secret.encode(), msg, hashlib.sha256).hexdigest()

    assert client.get("/payments/callback", params={**params, "razorpay_signature": "x"}).status_code == 400
    resp = client.get("/payments/callback", params={**params, "razorpay_signature": good})
    assert resp.status_code == 200 and "Payment received" in resp.text
    with factory() as s:
        assert s.get(Order, order["order_id"]).status == "paid"
