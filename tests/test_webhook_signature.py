# Valid signature accepted; tampered body or bad signature rejected with 400.
# Uses hmac.compare_digest over the RAW request body.
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from src import config
from src.db.models import AuditEvent, Order, Product, WebhookEvent
from src.payments.webhooks import app, verify_webhook_signature
from tests.helpers import cart_with, place


def _sign(raw: bytes, secret: str | None = None) -> str:
    key = (secret or config.settings.razorpay_webhook_secret).encode()
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def _event(event: str, order: dict, *, amount_paid: int | None = None, link_id=None) -> bytes:
    body = {
        "entity": "event",
        "event": event,
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {"entity": {
                "id": link_id or order["link_id"],
                "reference_id": order["order_id"],
                "amount": order["amount_paise"],
                "amount_paid": order["amount_paise"] if amount_paid is None else amount_paid,
                "currency": "INR",
                "status": event.split(".")[-1],
                "short_url": order["payment_link_url"],
            }},
            "payment": {"entity": {"id": "pay_test123", "status": "captured"}},
        },
        "created_at": 1790000000,
    }
    return json.dumps(body).encode()


def _post(client, raw: bytes, *, signature: str | None = None, event_id: str | None = "evt_1"):
    headers = {"Content-Type": "application/json",
               "X-Razorpay-Signature": _sign(raw) if signature is None else signature}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    return client.post("/webhooks/razorpay", content=raw, headers=headers)


@pytest.fixture
def placed(store):
    factory, gateway = store
    cart_id = cart_with(("SNK-THT-250", 2))
    res = place(factory, cart_id, "idem-webhook-01")
    assert res["ok"], res
    res["link_id"] = next(iter(gateway.links))
    return factory, res


def test_signature_helper():
    raw = b'{"a": 1}'
    assert verify_webhook_signature(raw, _sign(raw, "s3cret"), "s3cret")
    assert not verify_webhook_signature(raw + b" ", _sign(raw, "s3cret"), "s3cret")
    assert not verify_webhook_signature(raw, "", "s3cret")


def test_valid_paid_webhook_marks_order_paid(placed):
    factory, order = placed
    resp = _post(TestClient(app), _event("payment_link.paid", order))
    assert resp.status_code == 200 and resp.json()["status"] == "applied"
    with factory() as s:
        assert s.get(Order, order["order_id"]).status == "paid"


def test_forged_signature_is_rejected_and_logged(placed):
    # evals/scenarios/forged_webhook.yaml
    factory, order = placed
    resp = _post(TestClient(app), _event("payment_link.paid", order), signature="invalid")
    assert resp.status_code == 400
    with factory() as s:
        assert s.get(Order, order["order_id"]).status == "pending"
        last = s.query(AuditEvent).order_by(AuditEvent.id.desc()).first()
    assert last.action == "webhook.rejected" and last.outcome == "blocked"


def test_tampered_body_is_rejected(placed):
    _, order = placed
    raw = _event("payment_link.paid", order)
    signature = _sign(raw)
    tampered = raw.replace(b'"amount_paid": ', b'"amount_paid": 1')
    assert _post(TestClient(app), tampered, signature=signature).status_code == 400


def test_wrong_secret_is_rejected(placed):
    _, order = placed
    raw = _event("payment_link.paid", order)
    assert _post(TestClient(app), raw, signature=_sign(raw, "not-the-secret")).status_code == 400


def test_replayed_event_is_processed_once(placed):
    factory, order = placed
    client = TestClient(app)
    raw = _event("payment_link.paid", order)
    first = _post(client, raw, event_id="evt_replay")
    second = _post(client, raw, event_id="evt_replay")
    assert first.json()["status"] == "applied" and second.json()["status"] == "duplicate"
    with factory() as s:
        assert s.query(WebhookEvent).count() == 1


def test_amount_mismatch_is_refused(placed):
    factory, order = placed
    resp = _post(TestClient(app), _event("payment_link.paid", order, amount_paid=100))
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    with factory() as s:
        assert s.get(Order, order["order_id"]).status == "pending"


def test_link_for_another_order_is_refused(placed):
    _, order = placed
    resp = _post(TestClient(app), _event("payment_link.paid", order, link_id="plink_other"))
    # found via reference_id, but the stored link id differs
    assert resp.json()["status"] == "rejected"


def test_expired_webhook_releases_stock(placed):
    factory, order = placed
    resp = _post(TestClient(app), _event("payment_link.expired", order))
    assert resp.json()["status"] == "applied"
    with factory() as s:
        assert s.get(Order, order["order_id"]).status == "expired"
        assert s.get(Product, "SNK-THT-250").stock == 12


def test_cancelled_webhook(placed):
    _, order = placed
    assert _post(TestClient(app), _event("payment_link.cancelled", order)).json()["status"] == "applied"


def test_payment_after_expiry_is_flagged(placed):
    _, order = placed
    client = TestClient(app)
    _post(client, _event("payment_link.expired", order), event_id="evt_a")
    resp = _post(client, _event("payment_link.paid", order), event_id="evt_b")
    assert resp.json()["status"] == "attention"


@pytest.mark.parametrize("event", ["payment.failed", "payment.captured", "payment_link.partially_paid",
                                   "refund.processed", "something.new"])
def test_other_events_are_acknowledged_without_changes(placed, event):
    factory, order = placed
    resp = _post(TestClient(app), _event(event, order), event_id=f"evt_{event}")
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"
    with factory() as s:
        assert s.get(Order, order["order_id"]).status == "pending"


def test_order_status_endpoint(placed):
    _, order = placed
    client = TestClient(app)
    body = client.get(f"/orders/{order['order_id']}").json()
    assert body["status"] == "pending" and "customer_phone" not in body
    assert client.get("/orders/ord_nope").status_code == 404
