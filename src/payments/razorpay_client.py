# Thin wrapper over the Razorpay SDK (test mode only).
# create_payment_link(amount_paise, reference_id, customer, notes) -> link
# fetch_payment_link(link_id)
# NOTE: amounts are handled in PAISE here; convert at the boundary, exactly once.
#
# The rest of the code talks to the `PaymentGateway` protocol, never to the SDK directly, so
# tests and evals swap in `FakeRazorpay` and no network call or credential is needed.
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

# Razorpay requires expire_by to be at least 15 minutes in the future.
LINK_TTL_SECONDS = 30 * 60


@dataclass
class PaymentLink:
    id: str
    short_url: str
    status: str  # created | partially_paid | paid | expired | cancelled
    amount_paise: int
    reference_id: str


class PaymentGateway(Protocol):
    def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        customer_name: str,
        customer_phone: str,
        description: str,
        notes: dict | None = None,
    ) -> PaymentLink: ...

    def fetch_payment_link(self, link_id: str) -> PaymentLink: ...


def _to_link(raw: dict) -> PaymentLink:
    return PaymentLink(
        id=raw["id"],
        short_url=raw["short_url"],
        status=raw["status"],
        amount_paise=int(raw["amount"]),
        reference_id=raw.get("reference_id", ""),
    )


class RazorpayGateway:
    def __init__(self, key_id: str, key_secret: str, callback_url: str | None = None) -> None:
        if not key_id.startswith("rzp_test_"):
            # Second line of defence after src.config: this class never talks to live mode.
            raise RuntimeError("RazorpayGateway only accepts rzp_test_ keys.")
        import razorpay

        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.set_app_details({"title": "visala-agentic-commerce", "version": "0.1.0"})
        self._callback_url = callback_url

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        reference_id: str,
        customer_name: str,
        customer_phone: str,
        description: str,
        notes: dict | None = None,
    ) -> PaymentLink:
        if not isinstance(amount_paise, int) or amount_paise <= 0:
            raise ValueError("amount_paise must be a positive integer.")
        payload = {
            "amount": amount_paise,  # Razorpay expects the smallest unit: paise
            "currency": "INR",
            "accept_partial": False,
            "reference_id": reference_id,  # our order id; Razorpay rejects duplicates too
            "description": description[:2048],
            "customer": {"name": customer_name, "contact": f"+91{customer_phone}"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
            "expire_by": int(time.time()) + LINK_TTL_SECONDS,
            "notes": {k: str(v)[:256] for k, v in (notes or {}).items()},
        }
        if self._callback_url:
            payload["callback_url"] = self._callback_url
            payload["callback_method"] = "get"
        return _to_link(self._client.payment_link.create(payload))

    def fetch_payment_link(self, link_id: str) -> PaymentLink:
        return _to_link(self._client.payment_link.fetch(link_id))


@dataclass
class FakeRazorpay:
    # In-memory stand-in used by tests and evals. Records every call so tests can assert
    # "exactly one payment link was created".
    links: dict[str, PaymentLink] = field(default_factory=dict)
    calls: list[dict] = field(default_factory=list)
    fail_next: bool = False

    def create_payment_link(self, **kwargs) -> PaymentLink:
        self.calls.append(kwargs)
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("simulated Razorpay outage")
        if any(link.reference_id == kwargs["reference_id"] for link in self.links.values()):
            raise ValueError("reference_id already used")  # mirrors Razorpay's behaviour
        link_id = f"plink_{uuid.uuid4().hex[:14]}"
        link = PaymentLink(
            id=link_id,
            short_url=f"https://rzp.io/i/{link_id[-8:]}",
            status="created",
            amount_paise=kwargs["amount_paise"],
            reference_id=kwargs["reference_id"],
        )
        self.links[link_id] = link
        return link

    def fetch_payment_link(self, link_id: str) -> PaymentLink:
        return self.links[link_id]

    def mark(self, link_id: str, status: str) -> None:
        self.links[link_id].status = status


_gateway: PaymentGateway | None = None


def get_gateway() -> PaymentGateway:
    global _gateway
    if _gateway is None:
        from src.config import settings

        if settings.payment_gateway == "fake":
            _gateway = FakeRazorpay()
        else:
            _gateway = RazorpayGateway(
                settings.razorpay_key_id,
                settings.razorpay_key_secret,
                # After paying, Razorpay redirects the buyer's browser here with a signed status.
                callback_url=f"{settings.public_base_url.rstrip('/')}/payments/callback",
            )
    return _gateway


def set_gateway(gateway: PaymentGateway | None) -> None:
    # Used by tests/evals to inject FakeRazorpay; None resets to the real gateway.
    global _gateway
    _gateway = gateway
