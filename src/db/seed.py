# Loads data/catalog.json into the demo database.
#   python -m src.db.seed           upsert: safe to run repeatedly, never duplicates
#   python -m src.db.seed --reset   drop everything and start clean (used before eval runs)
import argparse
import json
from pathlib import Path

from sqlalchemy.orm import Session

from src.db.models import Product
from src.money import to_paise

CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "catalog.json"


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def seed_products(session: Session, catalog: dict) -> int:
    count = 0
    for p in catalog["products"]:
        # merge() inserts or updates by primary key -> re-running never duplicates.
        session.merge(
            Product(
                id=p["id"],
                name=p["name"],
                category=p["category"],
                price_paise=to_paise(p["price_inr"]),  # rupees -> paise, exactly once
                weight_g=p["weight_g"],
                stock=p["stock"],
                spice_level=p["spice_level"],
                shelf_life_days=p["shelf_life_days"],
                veg=p.get("veg", True),
                active=True,
            )
        )
        count += 1
    session.commit()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the demo database from data/catalog.json")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()

    from src.config import settings
    from src.db.session import init_db, make_engine, make_session_factory, reset_db

    engine = make_engine(settings.database_url)
    reset_db(engine) if args.reset else init_db(engine)

    with make_session_factory(engine)() as session:
        n = seed_products(session, load_catalog())
        in_stock = session.query(Product).filter(Product.stock > 0).count()

    print(f"Seeded {n} products into {settings.database_url} ({in_stock} in stock).")


if __name__ == "__main__":
    main()
