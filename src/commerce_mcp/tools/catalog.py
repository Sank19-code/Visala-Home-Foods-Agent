# Tools: search_products(query, max_price, category) -> list[Product]
#        get_product(product_id) -> Product
# Returns price, stock, weight, spice level, shelf life so the agent can reason without guessing.
#
# Out-of-stock products are still returned (with in_stock=false) unless the caller filters them
# out: the agent has to KNOW an item is unavailable to tell the user and offer a substitute.
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Product
from src.errors import InvalidInput, NotFound
from src.money import format_inr, to_paise

MAX_RESULTS = 20
_SPICE_ORDER = {"mild": 0, "medium": 1, "hot": 2}
# How shoppers phrase things -> the words the catalogue uses.
_SYNONYMS = {"spicy": "hot", "chilli": "hot", "chili": "hot", "powder": "podi", "achar": "pickle"}


def product_view(p: Product) -> dict:
    return {
        "product_id": p.id,
        "name": p.name,
        "category": p.category,
        "price_paise": p.price_paise,
        "price_display": format_inr(p.price_paise),
        "weight_g": p.weight_g,
        "in_stock": p.stock > 0,
        "stock": p.stock,
        "spice_level": p.spice_level,
        "shelf_life_days": p.shelf_life_days,
        "veg": p.veg,
    }


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Naive singularisation so "pickles" matches "pickle" and "podis" matches "podi".
    words = [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]
    return [_SYNONYMS.get(w, w) for w in words]


def _score(p: Product, tokens: list[str]) -> int:
    haystack = set(_tokens(f"{p.name} {p.category} {p.id.replace('-', ' ')} {p.spice_level}"))
    return sum(1 for t in tokens if t in haystack)


def search_products(
    session: Session,
    query: str | None = None,
    category: str | None = None,
    max_price_inr: int | None = None,
    spice_level: str | None = None,
    in_stock_only: bool = False,
    limit: int = 10,
) -> dict:
    if limit < 1 or limit > MAX_RESULTS:
        raise InvalidInput(f"limit must be between 1 and {MAX_RESULTS}.")
    if spice_level and spice_level not in _SPICE_ORDER:
        raise InvalidInput("spice_level must be one of: mild, medium, hot.")

    stmt = select(Product).where(Product.active.is_(True))
    if category:
        stmt = stmt.where(Product.category == category.strip().lower())
    if max_price_inr is not None:
        try:
            stmt = stmt.where(Product.price_paise <= to_paise(max_price_inr))
        except (TypeError, ValueError) as exc:
            raise InvalidInput(f"max_price_inr is not a valid amount: {exc}") from None
    if spice_level:
        stmt = stmt.where(Product.spice_level == spice_level)
    if in_stock_only:
        stmt = stmt.where(Product.stock > 0)

    products = session.scalars(stmt.order_by(Product.price_paise, Product.id)).all()

    tokens = _tokens(query or "")
    if tokens:
        scored = [(p, _score(p, tokens)) for p in products]
        products = [p for p, s in sorted(scored, key=lambda x: -x[1]) if s > 0]

    results = [product_view(p) for p in products[:limit]]
    return {
        "count": len(results),
        "products": results,
        "categories": sorted(set(session.scalars(select(Product.category).distinct()).all())),
    }


def get_product_or_raise(session: Session, product_id: str) -> Product:
    product = session.get(Product, (product_id or "").strip().upper())
    if product is None or not product.active:
        raise NotFound(
            f"No product with id {product_id!r}.",
            hint="Use search_products to find valid product ids.",
        )
    return product


def substitutes_for(session: Session, product: Product, limit: int = 3) -> list[dict]:
    # Same category, in stock, closest in price first. The agent must ASK before using one.
    rows = session.scalars(
        select(Product).where(
            Product.active.is_(True),
            Product.category == product.category,
            Product.id != product.id,
            Product.stock > 0,
        )
    ).all()
    rows = sorted(rows, key=lambda p: abs(p.price_paise - product.price_paise))
    return [product_view(p) for p in rows[:limit]]


def get_product(session: Session, product_id: str) -> dict:
    product = get_product_or_raise(session, product_id)
    view = product_view(product)
    if not view["in_stock"]:
        view["substitutes"] = substitutes_for(session, product)
        view["note"] = "Out of stock. Offer a substitute only after the user agrees."
    return view
