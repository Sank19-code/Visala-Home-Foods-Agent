# Shared test fixtures.
import os

import pytest

# Dummy TEST-mode credentials so any module that imports src.config can load in CI.
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_ci_dummy")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "ci_dummy_secret")
os.environ.setdefault("RAZORPAY_WEBHOOK_SECRET", "ci_dummy_webhook_secret")

from src.db.seed import load_catalog, seed_products  # noqa: E402
from src.db.session import init_db, make_engine, make_session_factory  # noqa: E402


@pytest.fixture
def db():
    # A fresh in-memory database per test: fast, isolated, no files left behind.
    engine = make_engine("sqlite://")
    init_db(engine)
    session = make_session_factory(engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def seeded_db(db):
    seed_products(db, load_catalog())
    return db
