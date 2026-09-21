# Money helpers. All amounts inside the system are integer PAISE.
# Rupees exist only at the edges: reading the catalogue and showing totals to a human.
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_paise(rupees: int | str | Decimal) -> int:
    # Floats are rejected on purpose: 0.1 + 0.2 != 0.3, and money must be exact.
    if isinstance(rupees, bool) or isinstance(rupees, float):
        raise TypeError("Pass rupees as int, str or Decimal — never float.")
    try:
        value = Decimal(str(rupees))
    except InvalidOperation as exc:
        raise ValueError(f"Not a valid rupee amount: {rupees!r}") from exc
    if value < 0:
        raise ValueError("Amounts cannot be negative.")
    paise = value * 100
    if paise != paise.to_integral_value():
        raise ValueError(f"{rupees!r} has fractions of a paisa.")
    return int(paise.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_inr(paise: int) -> str:
    rupees, rem = divmod(paise, 100)
    return f"₹{rupees:,}" if rem == 0 else f"₹{rupees:,}.{rem:02d}"
