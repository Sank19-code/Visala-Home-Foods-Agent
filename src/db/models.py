# Database schema for the demo store.
#
# Design rules enforced HERE, at the database level, not just in application code:
#   * money is integer paise                    -> no float rounding errors
#   * one order per cart                        -> UNIQUE(cart_id)
#   * one order per idempotency key             -> key is the PRIMARY KEY
#   * stock, prices and quantities are sane     -> CHECK constraints
#   * the audit log is append-only              -> triggers block UPDATE/DELETE (see session.py)
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price_paise >= 0", name="price_non_negative"),
        CheckConstraint("stock >= 0", name="stock_non_negative"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. PKL-MNG-250
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(40), index=True)
    price_paise: Mapped[int] = mapped_column(Integer)
    weight_g: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    spice_level: Mapped[str] = mapped_column(String(20))
    shelf_life_days: Mapped[int] = mapped_column(Integer)
    veg: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (
        CheckConstraint("status IN ('open','checked_out','abandoned')", name="cart_status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _id("cart"))
    customer_ref: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart", cascade="all, delete-orphan"
    )


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="one_line_per_product"),
        CheckConstraint("qty > 0", name="qty_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    qty: Mapped[int] = mapped_column(Integer)
    # Price seen when added. create_order re-checks against the live price anyway.
    unit_price_paise: Mapped[int] = mapped_column(Integer)

    cart: Mapped[Cart] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("cart_id", name="one_order_per_cart"),
        CheckConstraint("amount_paise > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('pending','paid','failed','expired','cancelled')", name="order_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: _id("ord"))
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id"))
    customer_ref: Mapped[str] = mapped_column(String(80), index=True)
    customer_name: Mapped[str] = mapped_column(String(120))
    customer_phone: Mapped[str] = mapped_column(String(20))
    pincode: Mapped[str] = mapped_column(String(10))
    items_paise: Mapped[int] = mapped_column(Integer)
    delivery_paise: Mapped[int] = mapped_column(Integer, default=0)
    amount_paise: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    razorpay_payment_link_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    payment_link_url: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyRecord(Base):
    # The key IS the primary key, so a second insert with the same key cannot succeed.
    # request_hash lets us reject the same key reused for a DIFFERENT request.
    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("outcome IN ('ok','blocked','error')", name="audit_outcome"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(40))  # buyer_agent | merchant_agent | webhook
    action: Mapped[str] = mapped_column(String(60))  # tool name or event type
    session_id: Mapped[str | None] = mapped_column(String(80), index=True)
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    outcome: Mapped[str] = mapped_column(String(10))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
