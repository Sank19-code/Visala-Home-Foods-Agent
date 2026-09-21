from decimal import Decimal

import pytest

from src.money import format_inr, to_paise


@pytest.mark.parametrize(
    "rupees, paise",
    [(180, 18000), ("180", 18000), ("180.50", 18050), (Decimal("0.01"), 1), (0, 0)],
)
def test_to_paise_converts_exactly(rupees, paise):
    assert to_paise(rupees) == paise


def test_floats_are_refused():
    # 0.1 + 0.2 == 0.30000000000000004 — money must never pass through a float.
    with pytest.raises(TypeError):
        to_paise(0.1 + 0.2)


@pytest.mark.parametrize("bad", ["-5", "12.345", "abc"])
def test_invalid_amounts_are_refused(bad):
    with pytest.raises(ValueError):
        to_paise(bad)


def test_format_inr():
    assert format_inr(55900) == "₹559"
    assert format_inr(18050) == "₹180.50"
    assert format_inr(123456700) == "₹1,234,567"
