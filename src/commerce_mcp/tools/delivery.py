# Tool: check_delivery(pincode, requested_date) -> {serviceable, charge_inr, eta_date}
#
# Rules come from the "delivery" block of data/catalog.json:
#   * serviceable if the 6-digit pincode starts with one of serviceable_pincode_prefixes
#   * flat base charge, free when the items total reaches free_above_inr
#   * earliest delivery = today (IST) + standard_days
from datetime import date, datetime, timedelta
from functools import lru_cache

from src.errors import InvalidInput, NotServiceable
from src.guardrails.spend_limits import IST
from src.money import format_inr, to_paise


@lru_cache(maxsize=1)
def delivery_rules() -> dict:
    from src.db.seed import load_catalog

    raw = load_catalog()["delivery"]
    return {
        "prefixes": tuple(raw["serviceable_pincode_prefixes"]),
        "base_paise": to_paise(raw["base_charge_inr"]),
        "free_above_paise": to_paise(raw["free_above_inr"]),
        "standard_days": int(raw["standard_days"]),
    }


def validate_pincode(pincode: str) -> str:
    pincode = (pincode or "").strip().replace(" ", "")
    if len(pincode) != 6 or not pincode.isdigit() or pincode[0] == "0":
        raise InvalidInput(f"{pincode!r} is not a valid 6-digit Indian pincode.")
    return pincode


def is_serviceable(pincode: str) -> bool:
    return pincode.startswith(delivery_rules()["prefixes"])


def delivery_charge_paise(items_paise: int) -> int:
    rules = delivery_rules()
    return 0 if items_paise >= rules["free_above_paise"] else rules["base_paise"]


def earliest_date(today: date | None = None) -> date:
    today = today or datetime.now(IST).date()
    return today + timedelta(days=delivery_rules()["standard_days"])


def require_serviceable(pincode: str) -> str:
    pincode = validate_pincode(pincode)
    if not is_serviceable(pincode):
        raise NotServiceable(
            f"We do not deliver to pincode {pincode} yet.",
            hint="Tell the user; do not guess a nearby pincode.",
            pincode=pincode,
        )
    return pincode


def check_delivery(
    pincode: str,
    requested_date: str | None = None,
    items_total_inr: int | None = None,
    today: date | None = None,
) -> dict:
    pincode = validate_pincode(pincode)
    rules = delivery_rules()
    if not is_serviceable(pincode):
        return {"pincode": pincode, "serviceable": False, "message": "Not serviceable yet."}

    eta = earliest_date(today)
    result = {
        "pincode": pincode,
        "serviceable": True,
        "earliest_delivery_date": eta.isoformat(),
        "base_charge_paise": rules["base_paise"],
        "free_delivery_above_paise": rules["free_above_paise"],
        "rule": (
            f"{format_inr(rules['base_paise'])} delivery, free on orders of "
            f"{format_inr(rules['free_above_paise'])} or more."
        ),
    }

    if items_total_inr is not None:
        try:
            charge = delivery_charge_paise(to_paise(items_total_inr))
        except (TypeError, ValueError) as exc:
            raise InvalidInput(f"items_total_inr is not a valid amount: {exc}") from None
        result["charge_paise"] = charge
        result["charge_display"] = format_inr(charge)

    if requested_date:
        try:
            wanted = date.fromisoformat(requested_date)
        except ValueError:
            raise InvalidInput("requested_date must be YYYY-MM-DD.") from None
        result["requested_date"] = wanted.isoformat()
        result["requested_date_possible"] = wanted >= eta
        if wanted < eta:
            result["message"] = (
                f"{wanted.isoformat()} is too early; the earliest delivery is {eta.isoformat()}. "
                "Ask the user whether the later date is acceptable."
            )
    return result
