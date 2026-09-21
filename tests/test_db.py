# The data layer's guarantees, tested at the database level.
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.db.models import AuditEvent, Cart, CartItem, IdempotencyRecord, Order, Product
from src.db.seed import load_catalog, seed_products


def _order(cart_id: str, **overrides) -> Order:
    fields = {
        "cart_id": cart_id,
        "customer_ref": "cust_1",
        "customer_name": "Test Buyer",
        "customer_phone": "9999999999",
        "pincode": "560001",
        "items_paise": 51000,
        "delivery_paise": 4900,
        "amount_paise": 55900,
    }
    fields.update(overrides)
    return Order(**fields)


# --- seeding ----------------------------------------------------------------


def test_seed_loads_every_catalogue_product(seeded_db):
    assert seeded_db.query(Product).count() == len(load_catalog()["products"])


def test_seed_is_idempotent(seeded_db):
    before = seeded_db.query(Product).count()
    seed_products(seeded_db, load_catalog())
    seed_products(seeded_db, load_catalog())
    assert seeded_db.query(Product).count() == before


def test_prices_are_stored_in_paise(seeded_db):
    mango = seeded_db.get(Product, "PKL-MNG-250")
    assert mango.price_paise == 18000  # ₹180 in the catalogue


def test_catalogue_has_an_out_of_stock_item_for_evals(seeded_db):
    assert seeded_db.query(Product).filter(Product.stock == 0).count() >= 1


# --- constraints --------------------------------------------------------------


def test_negative_stock_is_rejected(seeded_db):
    seeded_db.get(Product, "PKL-MNG-250").stock = -1
    with pytest.raises(IntegrityError):
        seeded_db.commit()


def test_zero_quantity_cart_line_is_rejected(seeded_db):
    cart = Cart(customer_ref="cust_1")
    seeded_db.add(cart)
    seeded_db.flush()
    seeded_db.add(CartItem(cart_id=cart.id, product_id="PKL-MNG-250", qty=0, unit_price_paise=18000))
    with pytest.raises(IntegrityError):
        seeded_db.commit()


def test_foreign_keys_are_enforced(db):
    # SQLite silently ignores FKs unless enabled per connection — make sure we did.
    db.add(CartItem(cart_id="cart_missing", product_id="NOPE", qty=1, unit_price_paise=100))
    with pytest.raises(IntegrityError):
        db.commit()


def test_one_order_per_cart(seeded_db):
    cart = Cart(customer_ref="cust_1")
    seeded_db.add(cart)
    seeded_db.flush()
    seeded_db.add(_order(cart.id))
    seeded_db.commit()
    seeded_db.add(_order(cart.id))
    with pytest.raises(IntegrityError):
        seeded_db.commit()


def test_idempotency_key_cannot_be_reused(seeded_db):
    cart = Cart(customer_ref="cust_1")
    seeded_db.add(cart)
    seeded_db.flush()
    order = _order(cart.id)
    seeded_db.add(order)
    seeded_db.flush()
    seeded_db.add(IdempotencyRecord(key="idem-123", request_hash="h1", order_id=order.id))
    seeded_db.commit()

    seeded_db.add(IdempotencyRecord(key="idem-123", request_hash="h1", order_id=order.id))
    with pytest.raises(IntegrityError):
        seeded_db.commit()


# --- audit log ----------------------------------------------------------------


def _audit(db) -> AuditEvent:
    event = AuditEvent(actor="buyer_agent", action="search_products", outcome="ok")
    db.add(event)
    db.commit()
    return event


def test_audit_log_rejects_updates(db):
    event = _audit(db)
    with pytest.raises(Exception, match="append-only"):
        db.execute(text("UPDATE audit_events SET outcome='error' WHERE id=:id"), {"id": event.id})


def test_audit_log_rejects_deletes(db):
    event = _audit(db)
    with pytest.raises(Exception, match="append-only"):
        db.execute(text("DELETE FROM audit_events WHERE id=:id"), {"id": event.id})
