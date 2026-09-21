# Engine and session setup.
# Deliberately does NOT import src.config at module level, so the schema can be used
# (and tested) without Razorpay credentials present.
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base

_APPEND_ONLY_TRIGGERS = [
    """CREATE TRIGGER IF NOT EXISTS audit_no_update
       BEFORE UPDATE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""",
    """CREATE TRIGGER IF NOT EXISTS audit_no_delete
       BEFORE DELETE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;""",
]


def make_engine(url: str) -> Engine:
    kwargs = {}
    if url in ("sqlite://", "sqlite:///:memory:"):
        # One shared connection, otherwise every session sees a fresh empty database.
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":
        # SQLite ignores foreign keys unless told otherwise, per connection.
        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            for ddl in _APPEND_ONLY_TRIGGERS:
                conn.execute(text(ddl))


def reset_db(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    init_db(engine)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_session() -> Session:
    # App-wide session using DATABASE_URL from .env.
    global _engine, _factory
    if _factory is None:
        from src.config import settings

        _engine = make_engine(settings.database_url)
        init_db(_engine)
        _factory = make_session_factory(_engine)
    return _factory()
