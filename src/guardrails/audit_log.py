# Append-only log of every tool call: who, tool, args, result, latency, timestamp.
# This is what makes the agent's behaviour replayable in the demo.
#
# Written through its OWN session, so a tool that fails and rolls back still leaves a record
# of the attempt. The table itself rejects UPDATE/DELETE (triggers in src/db/session.py).
import json
import re
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import AuditEvent

MAX_JSON_CHARS = 4000
_SECRET_KEYS = re.compile(r"token|secret|password|signature", re.IGNORECASE)
_PHONE_KEYS = re.compile(r"phone|contact", re.IGNORECASE)


def redact(value):
    # Secrets are dropped, phone numbers masked to the last 4 digits.
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "***" if v else v
            elif _PHONE_KEYS.search(str(k)) and isinstance(v, str) and len(v) > 4:
                out[k] = "*" * (len(v) - 4) + v[-4:]
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _dump(value) -> str:
    text = json.dumps(redact(value), default=str, ensure_ascii=False)
    return text if len(text) <= MAX_JSON_CHARS else text[: MAX_JSON_CHARS - 12] + '..."trunc"'


def record(
    session_factory: Callable[[], Session],
    *,
    actor: str,
    action: str,
    args: dict | None = None,
    result: dict | None = None,
    outcome: str = "ok",
    latency_ms: int | None = None,
    session_id: str | None = None,
) -> None:
    with session_factory() as s:
        s.add(
            AuditEvent(
                actor=actor,
                action=action,
                session_id=session_id,
                args_json=_dump(args or {}),
                result_json=_dump(result or {}),
                outcome=outcome,
                latency_ms=latency_ms,
            )
        )
        s.commit()


def recent(session: Session, session_id: str | None = None, limit: int = 50) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if session_id:
        stmt = stmt.where(AuditEvent.session_id == session_id)
    return list(reversed(session.scalars(stmt).all()))
