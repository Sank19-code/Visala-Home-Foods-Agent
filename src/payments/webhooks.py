# FastAPI app: Razorpay webhooks, order status, the human approval page and the payment callback.
#
#   uvicorn src.payments.webhooks:app --port 8000        (make run)
#
# Routes
#   POST /webhooks/razorpay          Razorpay -> us. Signature verified over the RAW body
#                                    (HMAC-SHA256, hmac.compare_digest). Bad signature -> 400 and
#                                    a "blocked" audit event. Replays deduped on X-Razorpay-Event-Id.
#   GET  /orders/{order_id}          Order status for the agent / a UI to poll (no customer PII).
#   GET  /approve/{cart_id}          Human approval page: the server-computed quote + Approve button.
#   POST /approve/{cart_id}          Records the approval; create_order then needs no token.
#   GET  /payments/callback          Where Razorpay redirects the buyer after paying. The query
#                                    string is signed with the key secret, so it is verified and
#                                    can mark the order paid even when webhooks cannot reach
#                                    localhost.
#   GET  /health
#
# Webhook events to enable in the Razorpay dashboard (Test mode -> Settings -> Webhooks):
#   payment_link.paid            -> order paid           (amount, currency, link id checked first)
#   payment_link.expired         -> order expired, reserved stock released
#   payment_link.cancelled       -> order cancelled, reserved stock released
#   payment_link.partially_paid  -> logged; should never happen (links are created with
#                                   accept_partial=false)
#   payment.failed               -> logged; the link stays open so the buyer can retry
#   payment.captured             -> logged; payment_link.paid is the source of truth
#   refund.processed / refund.failed -> logged for the merchant; refunds are not automated
# Any other event is acknowledged (200) and logged as ignored.
#
# Every 2xx tells Razorpay "delivered". We answer 200 for every correctly signed event, even ones
# we refuse to act on (e.g. an amount mismatch), because a retry would not change the answer;
# those are recorded as blocked in the audit log instead. Only a bad signature or an unreadable
# body gets a 4xx.
import hashlib
import hmac
import html
import json
import logging
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.commerce_mcp import runtime
from src.db.models import Cart, Order, WebhookEvent
from src.errors import ToolError
from src.guardrails import audit_log, confirmation, spend_limits
from src.money import format_inr

app = FastAPI(title="Visala Agentic Commerce")
log = logging.getLogger(__name__)


def _settings():
    from src.config import settings

    return settings


def _audit(action: str, *, actor: str = "webhook", outcome: str = "ok", args=None, result=None):
    try:
        audit_log.record(
            runtime.open_session,
            actor=actor,
            action=action,
            args=args or {},
            result=result or {},
            outcome=outcome,
        )
    except Exception:  # never fail a webhook delivery because logging failed
        log.warning("audit write failed for %s", action, exc_info=True)


# --- signatures ------------------------------------------------------------------------------


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    # Over the RAW bytes: re-serialising parsed JSON changes whitespace/key order and breaks it.
    if not signature or not secret:
        return False
    return hmac.compare_digest(_hmac_hex(secret, raw_body), signature)


def verify_callback_signature(params: dict, key_secret: str) -> bool:
    try:
        message = "|".join(
            [
                params["razorpay_payment_link_id"],
                params["razorpay_payment_link_reference_id"],
                params["razorpay_payment_link_status"],
                params["razorpay_payment_id"],
            ]
        ).encode()
        return hmac.compare_digest(_hmac_hex(key_secret, message), params["razorpay_signature"])
    except KeyError:
        return False


# --- event handlers --------------------------------------------------------------------------
# Each returns (outcome, order_id, detail). outcome: applied | ignored | rejected | attention


def _entity(payload: dict, name: str) -> dict:
    return ((payload.get("payload") or {}).get(name) or {}).get("entity") or {}


def _find_order(session: Session, link: dict) -> Order | None:
    order = None
    if link.get("id"):
        order = session.query(Order).filter_by(razorpay_payment_link_id=link["id"]).one_or_none()
    if order is None and link.get("reference_id"):
        # Link created but its id never saved (crash between the two writes).
        order = session.get(Order, link["reference_id"])
    return order


def _link_matches(order: Order, link: dict) -> str | None:
    if link.get("reference_id") and link["reference_id"] != order.id:
        return "reference_id does not match the order"
    if order.razorpay_payment_link_id and link.get("id") != order.razorpay_payment_link_id:
        return "payment link id does not match the order"
    return None


def _on_link_paid(session: Session, payload: dict):
    from src.commerce_mcp.tools.orders import apply_payment_status

    link = _entity(payload, "payment_link")
    order = _find_order(session, link)
    if order is None:
        return "rejected", None, "no order for this payment link"
    problem = _link_matches(order, link)
    if problem:
        return "rejected", order.id, problem
    if link.get("currency", "INR") != "INR":
        return "rejected", order.id, f"unexpected currency {link.get('currency')}"
    paid = int(link.get("amount_paid") or 0)
    if paid != order.amount_paise:
        return "rejected", order.id, f"amount_paid {paid} != order amount {order.amount_paise}"

    if not order.razorpay_payment_link_id and link.get("id"):
        order.razorpay_payment_link_id = link["id"]
        order.payment_link_url = link.get("short_url")
    if order.status in ("expired", "cancelled", "failed"):
        session.commit()
        return "attention", order.id, f"money received for a {order.status} order: refund or ship"
    changed = apply_payment_status(session, order, "paid")
    return ("applied" if changed else "ignored"), order.id, "paid" if changed else "already paid"


def _link_closed(new_status: str):
    def handler(session: Session, payload: dict):
        from src.commerce_mcp.tools.orders import apply_payment_status

        link = _entity(payload, "payment_link")
        order = _find_order(session, link)
        if order is None:
            return "rejected", None, "no order for this payment link"
        problem = _link_matches(order, link)
        if problem:
            return "rejected", order.id, problem
        changed = apply_payment_status(session, order, new_status)
        return ("applied" if changed else "ignored"), order.id, (
            f"{new_status}; stock released" if changed else f"order already {order.status}"
        )

    return handler


def _log_only(detail: str):
    def handler(session: Session, payload: dict):
        payment = _entity(payload, "payment")
        link = _entity(payload, "payment_link")
        order_id = link.get("reference_id") or (payment.get("notes") or {}).get("order_id")
        extra = payment.get("error_description") or payment.get("status") or ""
        return "ignored", order_id, f"{detail}{': ' + extra if extra else ''}"

    return handler


HANDLERS = {
    "payment_link.paid": _on_link_paid,
    "payment_link.expired": _link_closed("expired"),
    "payment_link.cancelled": _link_closed("cancelled"),
    "payment_link.partially_paid": _log_only("partial payment on a no-partial link"),
    "payment.failed": _log_only("payment attempt failed; link stays open for retry"),
    "payment.captured": _log_only("payment captured; payment_link.paid drives the order"),
    "payment.authorized": _log_only("payment authorized"),
    "order.paid": _log_only("razorpay order paid; payment_link.paid drives the order"),
    "refund.created": _log_only("refund created"),
    "refund.processed": _log_only("refund processed"),
    "refund.failed": _log_only("refund failed: needs merchant attention"),
}


def process_event(session: Session, payload: dict, event_id: str | None) -> dict[str, Any]:
    event = str(payload.get("event") or "unknown")

    if event_id and session.get(WebhookEvent, event_id) is not None:
        return {"status": "duplicate", "event": event, "event_id": event_id}

    handler = HANDLERS.get(event, lambda s, p: ("ignored", None, "event not handled"))
    outcome, order_id, detail = handler(session, payload)

    if event_id:
        try:
            session.add(
                WebhookEvent(
                    event_id=event_id[:80], event=event[:60], order_id=order_id,
                    outcome=outcome, detail=detail,
                )
            )
            session.commit()
        except IntegrityError:  # the same event delivered twice at the same moment
            session.rollback()
            return {"status": "duplicate", "event": event, "event_id": event_id}

    return {"status": outcome, "event": event, "order_id": order_id, "detail": detail}


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    event_id = request.headers.get("x-razorpay-event-id") or None

    if not verify_webhook_signature(raw, signature, _settings().razorpay_webhook_secret):
        _audit(
            "webhook.rejected",
            outcome="blocked",
            args={"event_id": event_id, "bytes": len(raw), "has_signature": bool(signature)},
            result={"reason": "invalid signature"},
        )
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        _audit("webhook.rejected", outcome="blocked", args={"event_id": event_id},
               result={"reason": "unreadable body"})
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    with runtime.open_session() as session:
        result = process_event(session, payload, event_id)

    audit_outcome = {"rejected": "blocked", "attention": "error"}.get(result["status"], "ok")
    _audit(f"webhook.{result['event']}"[:60], outcome=audit_outcome,
           args={"event_id": event_id}, result=result)
    return result


# --- order status ----------------------------------------------------------------------------


@app.get("/orders/{order_id}")
def order_status(order_id: str):
    with runtime.open_session() as session:
        order = session.get(Order, order_id)
        if order is None:
            return JSONResponse({"error": "order not found"}, status_code=404)
        return {
            "order_id": order.id,
            "status": order.status,
            "amount_paise": order.amount_paise,
            "amount_display": format_inr(order.amount_paise),
            "payment_link_url": order.payment_link_url,
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        }


@app.get("/health")
def health():
    return {"ok": True, "gateway": _settings().payment_gateway}


# --- human approval page -----------------------------------------------------------------------

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;color:#1c1c1c}}
h1{{font-size:1.3rem}} table{{width:100%;border-collapse:collapse;margin:16px 0}}
td{{padding:6px 0;border-bottom:1px solid #eee}} td.r{{text-align:right}}
.total td{{font-weight:600;border-bottom:none}} .muted{{color:#666;font-size:.9rem}}
.warn{{background:#fff4e5;padding:10px;border-radius:6px}}
button{{font-size:1rem;padding:10px 18px;border-radius:6px;border:1px solid #999;cursor:pointer}}
button.primary{{background:#1f6feb;color:#fff;border-color:#1f6feb}}
</style></head><body>{body}</body></html>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(_PAGE.format(title=html.escape(title), body=body), status_code=status)


def _quote_for(session: Session, cart_id: str, pincode: str):
    from src.commerce_mcp.tools.orders import compute_quote

    cart = session.get(Cart, cart_id)
    if cart is None:
        return None, "No such cart."
    try:
        return compute_quote(session, cart, pincode), None
    except ToolError as exc:
        return None, exc.message


@app.get("/approve/{cart_id}", response_class=HTMLResponse)
def approval_page(cart_id: str, pincode: str):
    with runtime.open_session() as session:
        q, error = _quote_for(session, cart_id, pincode)
        if error:
            return _page("Cannot approve", f"<h1>Cannot approve</h1><p>{html.escape(error)}</p>", 409)
        headroom = spend_limits.headroom(session, q.cart.customer_ref)

    rows = "".join(
        f"<tr><td>{ln['qty']} × {html.escape(ln['name'])}</td>"
        f"<td class='r'>{format_inr(ln['line_total_paise'])}</td></tr>"
        for ln in q.lines
    )
    delivery = "Free" if q.delivery_paise == 0 else format_inr(q.delivery_paise)
    over = q.amount_paise > min(headroom["max_order_paise"], headroom["remaining_today_paise"])
    action = (
        "<p class='warn'>This total is above your spend limit, so it cannot be approved.</p>"
        if over
        else f"""<form method="post">
<input type="hidden" name="pincode" value="{html.escape(q.pincode)}">
<input type="hidden" name="fingerprint" value="{q.fingerprint}">
<button class="primary" name="decision" value="approve">Approve {format_inr(q.amount_paise)}</button>
<button name="decision" value="decline">Decline</button></form>"""
    )
    body = f"""<h1>Approve this purchase?</h1>
<p class="muted">Your shopping assistant prepared this order at Visala Home Foods.
Nothing is charged here: after you approve, you get a Razorpay payment link.</p>
<table>{rows}<tr><td>Delivery to {html.escape(q.pincode)}</td><td class='r'>{delivery}</td></tr>
<tr class="total"><td>Total</td><td class='r'>{format_inr(q.amount_paise)}</td></tr></table>
{action}
<p class="muted">Prices and stock were checked just now. If the cart changes, this approval
no longer applies.</p>"""
    return _page("Approve purchase", body)


@app.post("/approve/{cart_id}", response_class=HTMLResponse)
def approval_submit(
    cart_id: str, pincode: str = Form(...), fingerprint: str = Form(...), decision: str = Form(...)
):
    with runtime.open_session() as session:
        q, error = _quote_for(session, cart_id, pincode)
        if error:
            return _page("Cannot approve", f"<h1>Cannot approve</h1><p>{html.escape(error)}</p>", 409)
        if decision != "approve":
            _audit("approval.declined", actor="human", args={"cart_id": cart_id},
                   result={"amount_paise": q.amount_paise})
            return _page("Declined", "<h1>Declined</h1><p>Nothing was ordered. "
                         "Tell your assistant what you would like to change.</p>")
        if not hmac.compare_digest(fingerprint, q.fingerprint):
            return _page(
                "Cart changed",
                "<h1>The cart changed</h1><p>Prices, items or delivery changed after this page "
                "was opened. Reload it to see the new total.</p>",
                409,
            )
        headroom = spend_limits.headroom(session, q.cart.customer_ref)
        if q.amount_paise > min(headroom["max_order_paise"], headroom["remaining_today_paise"]):
            return _page("Over limit", "<h1>Over the spend limit</h1>", 409)
        approval = confirmation.record_approval(session, cart_id, q.fingerprint, q.amount_paise)

    _audit("approval.approved", actor="human",
           args={"cart_id": cart_id, "pincode": pincode},
           result={"approval_id": approval.id, "amount_paise": q.amount_paise})
    return _page(
        "Approved",
        f"<h1>Approved {format_inr(q.amount_paise)}</h1><p>Go back to your assistant and tell it "
        "you approved. It will place the order and give you a Razorpay payment link.</p>"
        "<p class='muted'>This approval is valid for 15 minutes and only for this exact cart.</p>",
    )


# --- payment callback (browser redirect after paying) ----------------------------------------


@app.get("/payments/callback", response_class=HTMLResponse)
def payment_callback(request: Request):
    from src.commerce_mcp.tools.orders import apply_payment_status

    params = dict(request.query_params)
    if not verify_callback_signature(params, _settings().razorpay_key_secret):
        _audit("callback.rejected", outcome="blocked",
               args={k: v for k, v in params.items() if k != "razorpay_signature"},
               result={"reason": "invalid signature"})
        return _page("Invalid", "<h1>Could not verify this payment</h1>", 400)

    status = params["razorpay_payment_link_status"]
    with runtime.open_session() as session:
        order = _find_order(
            session,
            {"id": params["razorpay_payment_link_id"],
             "reference_id": params["razorpay_payment_link_reference_id"]},
        )
        if order is None or _link_matches(
            order, {"id": params["razorpay_payment_link_id"],
                    "reference_id": params["razorpay_payment_link_reference_id"]}
        ):
            return _page("Unknown order", "<h1>We could not find this order</h1>", 404)
        # A signed "paid" for a link we created with accept_partial=false means the full amount.
        if status == "paid":
            apply_payment_status(session, order, "paid")
        order_id, final, amount = order.id, order.status, order.amount_paise

    _audit("callback.payment", args={"order_id": order_id, "link_status": status},
           result={"order_status": final})
    if final == "paid":
        return _page("Paid", f"<h1>Payment received</h1><p>{format_inr(amount)} for order "
                     f"<code>{html.escape(order_id)}</code>. You can close this tab.</p>")
    return _page("Payment status", f"<h1>Payment {html.escape(status)}</h1>"
                 f"<p>Order <code>{html.escape(order_id)}</code> is {html.escape(final)}.</p>")
