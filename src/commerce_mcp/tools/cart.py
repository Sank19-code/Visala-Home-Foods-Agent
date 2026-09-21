# Tools: create_cart(customer_ref) -> cart_id
#        add_to_cart(cart_id, product_id, qty) -> cart summary with running total
#        update_cart_item(cart_id, product_id, qty) -> set a line's qty (0 removes it)
#        get_cart(cart_id)
#
# Only OPEN carts can change. Stock is checked on every change, but the authoritative check is
# the one create_order makes against live stock at checkout.
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.commerce_mcp.tools.catalog import get_product_or_raise, substitutes_for
from src.db.models import Cart, CartItem
from src.errors import Conflict, InvalidInput, NotFound, OutOfStock
from src.money import format_inr

MAX_QTY_PER_LINE = 10
MAX_LINES = 20
_CUSTOMER_REF_RE = re.compile(r"^[A-Za-z0-9_.:@\-]{3,80}$")


def cart_view(cart: Cart) -> dict:
    lines = [
        {
            "product_id": item.product_id,
            "name": item.product.name,
            "qty": item.qty,
            "unit_price_paise": item.unit_price_paise,
            "line_total_paise": item.qty * item.unit_price_paise,
            "line_total_display": format_inr(item.qty * item.unit_price_paise),
        }
        for item in sorted(cart.items, key=lambda i: i.product_id)
    ]
    items_paise = sum(line["line_total_paise"] for line in lines)
    return {
        "cart_id": cart.id,
        "customer_ref": cart.customer_ref,
        "status": cart.status,
        "lines": lines,
        "item_count": sum(line["qty"] for line in lines),
        "items_total_paise": items_paise,
        "items_total_display": format_inr(items_paise),
        "note": "Delivery is not included. Call get_checkout_quote for the payable total.",
    }


def get_cart_or_raise(session: Session, cart_id: str, *, must_be_open: bool = False) -> Cart:
    cart = session.get(Cart, (cart_id or "").strip())
    if cart is None:
        raise NotFound(f"No cart with id {cart_id!r}.", hint="Call create_cart first.")
    if must_be_open and cart.status != "open":
        raise Conflict(
            f"Cart {cart.id} is {cart.status} and can no longer be changed.",
            hint="Create a new cart for a new purchase.",
            cart_status=cart.status,
        )
    return cart


def _touch(cart: Cart) -> None:
    # Line changes do not fire Cart.onupdate; abandoned-cart detection needs this timestamp.
    cart.updated_at = datetime.now(timezone.utc)


def _validate_qty(qty: int, *, allow_zero: bool) -> None:
    lowest = 0 if allow_zero else 1
    if not isinstance(qty, int) or isinstance(qty, bool) or not lowest <= qty <= MAX_QTY_PER_LINE:
        raise InvalidInput(f"qty must be a whole number from {lowest} to {MAX_QTY_PER_LINE}.")


def create_cart(session: Session, customer_ref: str) -> dict:
    customer_ref = (customer_ref or "").strip()
    if not _CUSTOMER_REF_RE.match(customer_ref):
        raise InvalidInput("customer_ref must be 3-80 characters (letters, digits, _ . : @ -).")
    cart = Cart(customer_ref=customer_ref)
    session.add(cart)
    session.commit()
    return cart_view(cart)


def _set_line(session: Session, cart: Cart, product_id: str, new_qty: int) -> None:
    product = get_product_or_raise(session, product_id)
    line = next((i for i in cart.items if i.product_id == product.id), None)

    if new_qty == 0:
        if line is not None:
            cart.items.remove(line)
        return

    if new_qty > product.stock:
        raise OutOfStock(
            f"Only {product.stock} x {product.name} in stock; {new_qty} requested."
            if product.stock
            else f"{product.name} is out of stock.",
            hint="Tell the user. Offer a substitute ONLY after asking them.",
            product_id=product.id,
            available=product.stock,
            substitutes=substitutes_for(session, product),
        )
    if line is None:
        if len(cart.items) >= MAX_LINES:
            raise InvalidInput(f"A cart can hold at most {MAX_LINES} different products.")
        cart.items.append(
            CartItem(product_id=product.id, qty=new_qty, unit_price_paise=product.price_paise)
        )
    else:
        line.qty = new_qty
        line.unit_price_paise = product.price_paise  # always the live price


def add_to_cart(session: Session, cart_id: str, product_id: str, qty: int = 1) -> dict:
    _validate_qty(qty, allow_zero=False)
    cart = get_cart_or_raise(session, cart_id, must_be_open=True)
    existing = next((i.qty for i in cart.items if i.product_id == product_id.strip().upper()), 0)
    if existing + qty > MAX_QTY_PER_LINE:
        raise InvalidInput(f"At most {MAX_QTY_PER_LINE} of one product per order.")
    _set_line(session, cart, product_id, existing + qty)
    _touch(cart)
    session.commit()
    return cart_view(cart)


def update_cart_item(session: Session, cart_id: str, product_id: str, qty: int) -> dict:
    _validate_qty(qty, allow_zero=True)
    cart = get_cart_or_raise(session, cart_id, must_be_open=True)
    _set_line(session, cart, product_id, qty)
    _touch(cart)
    session.commit()
    return cart_view(cart)


def get_cart(session: Session, cart_id: str) -> dict:
    return cart_view(get_cart_or_raise(session, cart_id))
