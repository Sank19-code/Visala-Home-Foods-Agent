# One order per idempotency key.
# Store (idempotency_key -> order_id) with a UNIQUE constraint; on a repeat key return the
# ORIGINAL order instead of creating a new one. This is the fix for the duplicate-receipt-ID
# bug seen in production on Visala Home Foods.
#
# The same key reused for a DIFFERENT request (other cart, other customer details) is refused,
# not silently answered with the old order — that would hide a bug in the caller.
import hashlib
import json
import re

from sqlalchemy.orm import Session

from src.db.models import IdempotencyRecord, Order
from src.errors import IdempotencyConflict, InvalidInput

_KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-]{8,80}$")


def validate_key(key: str) -> str:
    if not key or not _KEY_RE.match(key):
        raise InvalidInput(
            "idempotency_key must be 8-80 characters of letters, digits, '_', '-', '.', ':'.",
            hint="Generate one key per purchase attempt (e.g. a UUID) and reuse it on retries.",
        )
    return key


def request_hash(**fields) -> str:
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def lookup(session: Session, key: str, req_hash: str) -> Order | None:
    # Returns the order already created for this key, or None if the key is new.
    record = session.get(IdempotencyRecord, key)
    if record is None:
        return None
    if record.request_hash != req_hash:
        raise IdempotencyConflict(
            "This idempotency_key was already used for a different order request.",
            hint="Use a new idempotency_key for a new purchase.",
            original_order_id=record.order_id,
        )
    return session.get(Order, record.order_id)


def record(session: Session, key: str, req_hash: str, order_id: str) -> None:
    # Added in the SAME transaction as the order. If two requests race, the primary key makes
    # the second commit fail, and the caller falls back to lookup().
    session.add(IdempotencyRecord(key=key, request_hash=req_hash, order_id=order_id))
