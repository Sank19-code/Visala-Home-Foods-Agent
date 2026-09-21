# Tools: get_checkout_quote(cart_id, pincode) -> exact payable total + what to show the user
#        create_order(cart_id, customer, idempotency_key, confirmation_token)
#            -> {order_id, payment_link_url}
#        get_order_status(order_id) -> pending | paid | failed | expired | cancelled
#
# create_order runs its checks in this order:
#   0. dedupe on idempotency_key     (src.guardrails.idempotency)
#      Runs FIRST so a replayed call returns the original order. If it ran after the spend cap,
#      a retry would count the first order against the daily cap and be wrongly refused.
#   1. re-check every line item's live price and stock (never trust the agent's earlier view)
#   2. enforce spend caps           (src.guardrails.spend_limits)
#   3. require explicit confirmation (src.guardrails.confirmation)
#   4. write order + idempotency record + stock decrement in ONE transaction
#   5. create the Razorpay payment link (test mode), reference_id = our order id
#   6. write an audit event          (done for every tool by src.commerce_mcp.runtime)
#
# The agent never pays. It gets a payment link and hands it to the human.
import re
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.commerce_mcp.tools.cart import get_cart_or_raise
from src.commerce_mcp.tools.catalog import substitutes_for
from src.commerce_mcp.tools.delivery import delivery_charge_paise, require_serviceable
from src.db.models import Cart, Order, Product
from src.errors import (
    Conflict,
    InvalidInput,
    NotFound,
    OutOfStock,
    PaymentLinkFailed,
    PriceChanged,
)
from src.guardrails import confirmation, idempotency, spend_limits
from src.money import format_inr
from src.payments.razorpay_client import PaymentGateway, get_gateway

_PHONE_RE = re.compile(r"^[6-9]\d{9}$")

# Razorpay payment-link status -> our order status
LINK_STATUS_MAP = {
    "created": "pending",
    "partially_paid": "pending",
    "paid": "paid",
    "expired": "expired",
    "cancelled": "cancelled",
}


# --- quote ----------------------------------------------------------------------------------


@dataclass
class Quote:
    cart: Cart
    pincode: str
    lines: list[dict]
    items_paise: int
    delivery_paise: int
    amount_paise: int
    fingerprint: str
    price_changes: list[dict]


def compute_quote(session: Session, cart: Cart, pincode: str) -> Quote:
    # Always from LIVE product rows, never from what the agent (or the cart) saw earlier.
    if cart.status != "open":
        raise Conflict(f"Cart {cart.id} is {cart.status}.", cart_status=cart.status)
    if not cart.items:
        raise InvalidInput("The cart is empty.", hint="Add products with add_to_cart first.")
    pincode = require_serviceable(pincode)

    lines, changes = [], []
    for item in sorted(cart.items, key=lambda i: i.product_id):
        product: Product = session.get(Product, item.product_id, populate_existing=True)
        if product is None or not product.active:
            raise OutOfStock(
                f"{item.product_id} is no longer sold.",
                hint="Remove it with update_cart_item(qty=0) and tell the user.",
                product_id=item.product_id,
            )
        if product.stock < item.qty:
            raise OutOfStock(
                f"Only {product.stock} x {product.name} left; the cart has {item.qty}.",
                hint="Tell the user. Offer a substitute ONLY after asking them.",
                product_id=product.id,
                available=product.stock,
                substitutes=substitutes_for(session, product),
            )
        if product.price_paise != item.unit_price_paise:
            changes.append(
                {
                    "product_id": product.id,
                    "old_unit_price_paise": item.unit_price_paise,
                    "new_unit_price_paise": product.price_paise,
                }
            )
        lines.append(
            {
                "product_id": product.id,
                "name": product.name,
                "qty": item.qty,
                "unit_price_paise": product.price_paise,
                "line_total_paise": item.qty * product.price_paise,
            }
        )

    items_paise = sum(line["line_total_paise"] for line in lines)
    delivery_paise = delivery_charge_paise(items_paise)
    amount = items_paise + delivery_paise
    fp = confirmation.fingerprint(
        cart.id, [(ln["product_id"], ln["qty"], ln["unit_price_paise"]) for ln in lines], pincode, amount
    )
    return Quote(cart, pincode, lines, items_paise, delivery_paise, amount, fp, changes)


def _reprice_cart(cart: Cart, quote: Quote) -> None:
    live = {ln["product_id"]: ln["unit_price_paise"] for ln in quote.lines}
    for item in cart.items:
        item.unit_price_paise = live[item.product_id]


def _summary_text(q: Quote) -> str:
    parts = [f"{ln['qty']} x {ln['name']} ({format_inr(ln['unit_price_paise'])})" for ln in q.lines]
    delivery = "free delivery" if q.delivery_paise == 0 else f"delivery {format_inr(q.delivery_paise)}"
    return f"{', '.join(parts)}; {delivery}; total {format_inr(q.amount_paise)} to pincode {q.pincode}."


def get_checkout_quote(session: Session, cart_id: str, pincode: str) -> dict:
    cart = get_cart_or_raise(session, cart_id, must_be_open=True)
    q = compute_quote(session, cart, pincode)
    if q.price_changes:
        _reprice_cart(cart, q)
        session.commit()

    limits = spend_limits.headroom(session, cart.customer_ref)
    within = (
        q.amount_paise <= limits["max_order_paise"]
        and q.amount_paise <= limits["remaining_today_paise"]
    )
    from src.config import settings

    return {
        "cart_id": cart.id,
        "pincode": q.pincode,
        "lines": q.lines,
        "items_total_paise": q.items_paise,
        "delivery_paise": q.delivery_paise,
        "amount_paise": q.amount_paise,
        "amount_display": format_inr(q.amount_paise),
        "summary": _summary_text(q),
        "price_changes": q.price_changes,
        "within_spend_limits": within,
        "spend_limits": limits,
        "requires_confirmation": settings.require_user_confirmation,
        "next_step": (
            "Show the user `summary` word for word and ask them to approve this exact total. "
            "Only their approval produces the confirmation_token needed by create_order."
            if within
            else "This total is above the spend limit. Reduce the cart before asking the user."
        ),
    }


def approve_quote(session: Session, cart_id: str, pincode: str) -> dict:
    # HUMAN-SIDE ONLY. Called by the buyer CLI / UI after the person says yes.
    # Not registered as an MCP tool, so the agent cannot approve its own purchase.
    cart = get_cart_or_raise(session, cart_id, must_be_open=True)
    q = compute_quote(session, cart, pincode)
    return {
        "confirmation_token": confirmation.issue_token(cart.id, q.fingerprint, q.amount_paise),
        "amount_paise": q.amount_paise,
        "summary": _summary_text(q),
    }


# --- orders ---------------------------------------------------------------------------------


def order_view(order: Order, **extra) -> dict:
    view = {
        "order_id": order.id,
        "status": order.status,
        "cart_id": order.cart_id,
        "items_total_paise": order.items_paise,
        "delivery_paise": order.delivery_paise,
        "amount_paise": order.amount_paise,
        "amount_display": format_inr(order.amount_paise),
        "pincode": order.pincode,
        "payment_link_url": order.payment_link_url,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
    }
    view.update(extra)
    return view


def _validate_customer(name: str, phone: str) -> tuple[str, str]:
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 120:
        raise InvalidInput("customer_name must be 2-120 characters.")
    phone = re.sub(r"[\s\-]", "", phone or "")
    phone = phone.removeprefix("+91")
    if not _PHONE_RE.match(phone):
        raise InvalidInput("customer_phone must be a 10-digit Indian mobile number.")
    return name, phone


def _attach_payment_link(session: Session, order: Order, gateway: PaymentGateway) -> None:
    try:
        link = gateway.create_payment_link(
            amount_paise=order.amount_paise,
            reference_id=order.id,
            customer_name=order.customer_name,
            customer_phone=order.customer_phone,
            description=f"Visala Home Foods order {order.id}",
            notes={"order_id": order.id, "cart_id": order.cart_id, "source": "buyer_agent"},
        )
    except Exception as exc:  # noqa: BLE001 — network, auth, Razorpay validation → PaymentLinkFailed
        raise PaymentLinkFailed(
            "The order was saved but the payment link could not be created.",
            hint="Retry create_order with the SAME idempotency_key; it will not duplicate the order.",
            order_id=order.id,
            reason=str(exc)[:200],
        ) from None
    if link.amount_paise != order.amount_paise:
        raise PaymentLinkFailed("Payment link amount does not match the order.", order_id=order.id)
    order.razorpay_payment_link_id = link.id
    order.payment_link_url = link.short_url
    session.commit()


def _ensure_link(session: Session, order: Order, gateway: PaymentGateway) -> None:
    if order.status == "pending" and not order.payment_link_url:
        _attach_payment_link(session, order, gateway)


def _decrement_stock(session: Session, product_id: str, qty: int) -> None:
    # Conditional UPDATE: atomic, never goes below zero even if two orders race.
    result = session.execute(
        update(Product)
        .where(Product.id == product_id, Product.stock >= qty)
        .values(stock=Product.stock - qty)
    )
    if result.rowcount != 1:
        raise OutOfStock(f"{product_id} sold out while the order was being placed.", product_id=product_id)


def create_order(
    session: Session,
    cart_id: str,
    customer_name: str,
    customer_phone: str,
    pincode: str,
    idempotency_key: str,
    confirmation_token: str | None = None,
    gateway: PaymentGateway | None = None,
) -> dict:
    gateway = gateway or get_gateway()
    from src.config import settings

    # 0. idempotency — a replay returns the original order, nothing else runs
    key = idempotency.validate_key(idempotency_key)
    name, phone = _validate_customer(customer_name, customer_phone)
    req_hash = idempotency.request_hash(
        cart_id=cart_id, customer_name=name, customer_phone=phone, pincode=(pincode or "").strip()
    )
    existing = idempotency.lookup(session, key, req_hash)
    if existing is not None:
        _ensure_link(session, existing, gateway)
        return order_view(existing, replayed=True)

    cart = get_cart_or_raise(session, cart_id)
    if cart.status == "checked_out":
        raise Conflict(
            f"Cart {cart.id} already has an order.",
            hint="To retry the same purchase, reuse the ORIGINAL idempotency_key.",
        )
    if cart.status != "open":
        raise Conflict(f"Cart {cart.id} is {cart.status}.")

    # 1. live price + stock
    q = compute_quote(session, cart, pincode)
    if q.price_changes:
        _reprice_cart(cart, q)
        session.commit()
        raise PriceChanged(
            "Prices changed since the cart was built.",
            hint="Call get_checkout_quote and get the user's approval for the new total.",
            price_changes=q.price_changes,
            new_amount_paise=q.amount_paise,
        )

    # 2. spend caps
    spend_limits.enforce(session, cart.customer_ref, q.amount_paise)

    # 3. explicit human confirmation for THIS cart, pincode and total
    if settings.require_user_confirmation:
        confirmation.verify_token(confirmation_token, cart.id, q.fingerprint, q.amount_paise)

    # 4. one transaction: order + idempotency record + stock + cart status
    order = Order(
        cart_id=cart.id,
        customer_ref=cart.customer_ref,
        customer_name=name,
        customer_phone=phone,
        pincode=q.pincode,
        items_paise=q.items_paise,
        delivery_paise=q.delivery_paise,
        amount_paise=q.amount_paise,
        status="pending",
    )
    try:
        session.add(order)
        session.flush()
        idempotency.record(session, key, req_hash, order.id)
        for line in q.lines:
            _decrement_stock(session, line["product_id"], line["qty"])
        cart.status = "checked_out"
        session.commit()
    except IntegrityError:
        # Lost a race with an identical request: return the winner's order.
        session.rollback()
        winner = idempotency.lookup(session, key, req_hash)
        if winner is not None:
            _ensure_link(session, winner, gateway)
            return order_view(winner, replayed=True)
        raise Conflict(f"Cart {cart_id} already has an order.") from None
    except OutOfStock:
        session.rollback()
        raise

    # 5. payment link
    _attach_payment_link(session, order, gateway)
    return order_view(
        order,
        replayed=False,
        next_step=(
            "Give payment_link_url to the user to pay. Do not attempt payment yourself. "
            "Then poll get_order_status until the status is paid."
        ),
    )


def apply_payment_status(session: Session, order: Order, new_status: str) -> bool:
    # Single place where an order leaves 'pending'. Used by get_order_status (polling) and the
    # webhook handler. Returns True if the status changed. Terminal states never change.
    if new_status == order.status or order.status != "pending" or new_status == "pending":
        return False
    if new_status not in ("paid", "failed", "expired", "cancelled"):
        raise ValueError(f"Unknown order status {new_status!r}")
    order.status = new_status
    if new_status == "paid":
        from datetime import datetime, timezone

        order.paid_at = datetime.now(timezone.utc)
    else:
        # Unpaid: release the stock that was reserved at order time.
        cart = session.get(Cart, order.cart_id)
        for item in cart.items:
            session.execute(
                update(Product)
                .where(Product.id == item.product_id)
                .values(stock=Product.stock + item.qty)
            )
    session.commit()
    return True


def get_order_status(
    session: Session, order_id: str, refresh: bool = True, gateway: PaymentGateway | None = None
) -> dict:
    order = session.get(Order, (order_id or "").strip())
    if order is None:
        raise NotFound(f"No order with id {order_id!r}.")

    note = None
    if refresh and order.status == "pending" and order.razorpay_payment_link_id:
        # Fallback for when webhooks cannot reach this machine (e.g. running on localhost).
        try:
            link = (gateway or get_gateway()).fetch_payment_link(order.razorpay_payment_link_id)
            apply_payment_status(session, order, LINK_STATUS_MAP.get(link.status, "pending"))
        except Exception as exc:  # noqa: BLE001 — refresh is best-effort; fall back to stored status
            note = f"Could not refresh from Razorpay ({type(exc).__name__}); showing stored status."

    extra = {"message": _status_message(order.status)}
    if note:
        extra["note"] = note
    return order_view(order, **extra)


def _status_message(status: str) -> str:
    return {
        "pending": "Waiting for the user to pay using the payment link.",
        "paid": "Paid. Confirm the order to the user.",
        "expired": "The payment link expired unpaid. Ask the user before creating a new order.",
        "cancelled": "The payment link was cancelled.",
        "failed": "Payment failed. Ask the user whether to try again.",
    }[status]

