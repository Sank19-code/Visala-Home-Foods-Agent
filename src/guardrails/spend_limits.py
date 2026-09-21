# Per-order and per-customer-per-day spend caps. Raises SpendLimitExceeded.
#
# The daily cap counts orders that are pending OR paid: an agent that opens five unpaid
# payment links in a row is still trying to spend that money, so pending links use up budget.
# The "day" is the Indian calendar day (IST), because that is how the merchant and the
# customer think about "today".
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import Order
from src.errors import SpendLimitExceeded
from src.money import format_inr, to_paise

IST = timezone(timedelta(hours=5, minutes=30))
COUNTED_STATUSES = ("pending", "paid")


def _limits() -> tuple[int, int]:
    from src.config import settings

    return to_paise(settings.max_order_amount_inr), to_paise(settings.max_daily_amount_inr)


def ist_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    # Start and end of the current IST day, returned in UTC.
    now = now or datetime.now(timezone.utc)
    start_ist = datetime.combine(now.astimezone(IST).date(), time.min, tzinfo=IST)
    return start_ist.astimezone(timezone.utc), (start_ist + timedelta(days=1)).astimezone(
        timezone.utc
    )


def spent_today_paise(session: Session, customer_ref: str, now: datetime | None = None) -> int:
    start, end = ist_day_bounds(now)
    total = session.scalar(
        select(func.coalesce(func.sum(Order.amount_paise), 0)).where(
            Order.customer_ref == customer_ref,
            Order.status.in_(COUNTED_STATUSES),
            Order.created_at >= start,
            Order.created_at < end,
        )
    )
    return int(total or 0)


def check_order_amount(amount_paise: int, max_order_paise: int | None = None) -> None:
    if max_order_paise is None:
        max_order_paise, _ = _limits()
    if amount_paise > max_order_paise:
        raise SpendLimitExceeded(
            f"Order total {format_inr(amount_paise)} is above the per-order limit of "
            f"{format_inr(max_order_paise)}.",
            hint="Remove items or reduce quantities, then quote again.",
            amount_paise=amount_paise,
            limit_paise=max_order_paise,
            limit="per_order",
        )


def check_daily_amount(
    session: Session,
    customer_ref: str,
    amount_paise: int,
    max_daily_paise: int | None = None,
    now: datetime | None = None,
) -> None:
    if max_daily_paise is None:
        _, max_daily_paise = _limits()
    spent = spent_today_paise(session, customer_ref, now)
    if spent + amount_paise > max_daily_paise:
        raise SpendLimitExceeded(
            f"This order would bring today's spend to {format_inr(spent + amount_paise)}, "
            f"above the daily limit of {format_inr(max_daily_paise)}.",
            hint="Tell the user the daily limit has been reached; do not split the order.",
            spent_today_paise=spent,
            amount_paise=amount_paise,
            limit_paise=max_daily_paise,
            limit="per_day",
        )


def enforce(session: Session, customer_ref: str, amount_paise: int, **overrides) -> None:
    check_order_amount(amount_paise, overrides.get("max_order_paise"))
    check_daily_amount(
        session, customer_ref, amount_paise, overrides.get("max_daily_paise"), overrides.get("now")
    )


def headroom(session: Session, customer_ref: str) -> dict:
    # What the agent may still spend — shown in quotes so it can plan within the caps.
    max_order, max_daily = _limits()
    spent = spent_today_paise(session, customer_ref)
    return {
        "max_order_paise": max_order,
        "remaining_today_paise": max(0, max_daily - spent),
    }
