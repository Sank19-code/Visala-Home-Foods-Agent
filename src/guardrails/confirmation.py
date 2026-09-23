# No order is created without an explicit user confirmation token for that exact cart total.
# Changing the cart invalidates the token.
#
# How it works
#   * `get_checkout_quote` (MCP tool) computes a quote and a `fingerprint`: a hash over the cart
#     id, every line (product, qty, live unit price), the pincode and the total.
#   * The HUMAN approves that quote outside the agent (the buyer CLI prompt, or a UI button).
#     That approval calls `issue_token(...)`, which signs the fingerprint with a server-side
#     secret. The LLM never sees the secret, so it cannot mint a token for itself.
#   * `create_order` recomputes the fingerprint from the live cart and calls `verify_token`.
#     Any change — a qty, a price, the pincode, the total — gives a different fingerprint,
#     and the old token no longer matches.
#
# Deliberately NOT exposed as an MCP tool: a tool the agent can call to "confirm" would be
# a confirmation the agent gives itself.
#
# Two ways for the human to approve, both outside the LLM:
#   1. Buyer CLI (src/buyer_agent): the person types "yes" in the terminal; the CLI host calls
#      issue_token and passes the token along with create_order.
#   2. Approval page (src/payments/webhooks.py, GET /approve/<cart_id>): for MCP clients such as
#      Claude Desktop that cannot show our prompt. Clicking Approve stores a PurchaseApproval
#      row; create_order then accepts it with no token.
import base64
import hashlib
import hmac
import json
import time

from src.errors import ConfirmationRequired

TOKEN_TTL_SECONDS = 15 * 60


def _secret() -> bytes:
    from src.config import settings

    # Derived from the key secret so no extra env var is needed; the label keeps it distinct
    # from the value Razorpay itself uses.
    return hmac.new(
        settings.razorpay_key_secret.encode(), b"visala-confirmation-v1", hashlib.sha256
    ).digest()


def fingerprint(cart_id: str, lines: list[tuple[str, int, int]], pincode: str, amount_paise: int) -> str:
    # lines: (product_id, qty, unit_price_paise). Order-independent.
    payload = {
        "cart_id": cart_id,
        "lines": sorted([list(line) for line in lines]),
        "pincode": pincode,
        "amount_paise": amount_paise,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_token(
    cart_id: str, fp: str, amount_paise: int, *, ttl: int = TOKEN_TTL_SECONDS, now: float | None = None
) -> str:
    body = {"c": cart_id, "f": fp, "a": amount_paise, "exp": int((now or time.time()) + ttl)}
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(
    token: str | None, cart_id: str, fp: str, amount_paise: int, *, now: float | None = None
) -> None:
    hint = (
        "Show the user the exact quote from get_checkout_quote and ask them to approve it. "
        "Only the user's approval produces a confirmation_token."
    )
    if not token:
        raise ConfirmationRequired("This order needs the user's explicit confirmation.", hint=hint)
    try:
        payload, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        body = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        raise ConfirmationRequired("The confirmation token is not valid.", hint=hint) from None

    if body.get("exp", 0) < (now or time.time()):
        raise ConfirmationRequired("The confirmation has expired.", hint=hint)
    if body.get("c") != cart_id:
        raise ConfirmationRequired("The confirmation was given for a different cart.", hint=hint)
    if body.get("f") != fp or body.get("a") != amount_paise:
        raise ConfirmationRequired(
            "The cart, delivery pincode or total changed after the user confirmed.",
            hint="Quote again and get a fresh confirmation for the new total.",
            confirmed_amount_paise=body.get("a"),
            current_amount_paise=amount_paise,
        )


# --- stored approvals (approval page) ----------------------------------------------------------


def record_approval(session, cart_id: str, fp: str, amount_paise: int, *, ttl: int = TOKEN_TTL_SECONDS):
    from datetime import datetime, timedelta, timezone

    from src.db.models import PurchaseApproval

    approval = PurchaseApproval(
        cart_id=cart_id,
        fingerprint=fp,
        amount_paise=amount_paise,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl),
    )
    session.add(approval)
    session.commit()
    return approval


def has_stored_approval(session, cart_id: str, fp: str, amount_paise: int) -> bool:
    from datetime import datetime, timezone

    from sqlalchemy import select

    from src.db.models import PurchaseApproval

    found = session.scalar(
        select(PurchaseApproval.id).where(
            PurchaseApproval.cart_id == cart_id,
            PurchaseApproval.fingerprint == fp,
            PurchaseApproval.amount_paise == amount_paise,
            PurchaseApproval.expires_at > datetime.now(timezone.utc),
        )
    )
    return found is not None


def require_confirmation(
    session, token: str | None, cart_id: str, fp: str, amount_paise: int, approval_url: str = ""
) -> str:
    # Returns how the order was confirmed ("token" | "approval_page"); raises otherwise.
    if token:
        verify_token(token, cart_id, fp, amount_paise)
        return "token"
    if has_stored_approval(session, cart_id, fp, amount_paise):
        return "approval_page"
    raise ConfirmationRequired(
        "This order needs the user's explicit confirmation.",
        hint=(
            f"Ask the user to open {approval_url} and approve the exact total, then call "
            "create_order again (no token needed after they approve there)."
            if approval_url
            else "Show the user the exact quote and get their approval first."
        ),
        approval_url=approval_url or None,
    )
