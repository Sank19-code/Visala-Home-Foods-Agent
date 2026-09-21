# Typed errors shared by the MCP tools and the guardrails.
#
# Tools RAISE these; the MCP server turns them into a structured payload the agent can act on:
#     {"ok": false, "error": {"code": "...", "message": "...", "hint": "...", "details": {...}}}
# A stable machine-readable `code` matters more than the message: the buyer agent branches on
# it (offer a substitute, re-quote, ask the user) and the eval suite asserts on it.


class ToolError(Exception):
    code = "tool_error"
    # Audit outcome: "blocked" for guardrail refusals, "error" for everything else.
    outcome = "error"

    def __init__(self, message: str, *, hint: str | None = None, **details) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        if self.details:
            err["details"] = self.details
        return err


class InvalidInput(ToolError):
    code = "invalid_input"


class NotFound(ToolError):
    code = "not_found"


class Conflict(ToolError):
    code = "conflict"


class OutOfStock(ToolError):
    code = "out_of_stock"


class PriceChanged(ToolError):
    code = "price_changed"


class NotServiceable(ToolError):
    code = "not_serviceable"


class PaymentLinkFailed(ToolError):
    code = "payment_link_failed"


# --- guardrail refusals -------------------------------------------------------------------


class GuardrailViolation(ToolError):
    code = "guardrail_violation"
    outcome = "blocked"


class SpendLimitExceeded(GuardrailViolation):
    code = "spend_limit_exceeded"


class ConfirmationRequired(GuardrailViolation):
    code = "confirmation_required"


class IdempotencyConflict(GuardrailViolation):
    code = "idempotency_conflict"
