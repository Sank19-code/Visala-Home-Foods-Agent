# Runs one tool call: opens a DB session, calls the tool, converts errors into a structured
# payload, and writes an audit event — for EVERY call, successful or not.
#
# Kept separate from server.py so tests and evals can drive tools exactly as the MCP server
# does, without an MCP client.
import os
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from src.errors import ToolError
from src.guardrails import audit_log

# Groups all calls from one server process in the audit log. Override with AGENT_SESSION_ID
# so an eval run can tag its own calls.
SESSION_ID = os.environ.get("AGENT_SESSION_ID") or f"mcp_{uuid.uuid4().hex[:12]}"
ACTOR = "buyer_agent"


def _default_factory() -> Session:
    from src.db.session import get_session

    return get_session()


_session_factory: Callable[[], Session] = _default_factory


def set_session_factory(factory: Callable[[], Session] | None) -> None:
    global _session_factory
    _session_factory = factory or _default_factory


def open_session() -> Session:
    # Same database the tools use. The webhook app and the buyer CLI go through this too.
    return _session_factory()


def run_tool(action: str, fn: Callable[..., dict], /, **kwargs: Any) -> dict:
    started = time.perf_counter()
    outcome = "ok"
    with _session_factory() as session:
        try:
            result = {"ok": True, **fn(session, **kwargs)}
        except ToolError as exc:
            session.rollback()
            outcome = exc.outcome
            result = {"ok": False, "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 — tool boundary: never leak a traceback to the agent
            session.rollback()
            outcome = "error"
            result = {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "Something went wrong on the store's side.",
                    "details": {"type": type(exc).__name__},
                },
            }

    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        audit_log.record(
            _session_factory,
            actor=ACTOR,
            action=action,
            args=kwargs,
            result=result,
            outcome=outcome,
            latency_ms=latency_ms,
            session_id=SESSION_ID,
        )
    except Exception as exc:  # noqa: BLE001 — logging must never fail a completed purchase
        # Never turn a completed purchase into a failure because logging broke — but say so.
        # (stderr is safe: stdout carries the MCP protocol.)
        print(f"[audit] failed to record {action}: {exc!r}", file=sys.stderr)
    return result
