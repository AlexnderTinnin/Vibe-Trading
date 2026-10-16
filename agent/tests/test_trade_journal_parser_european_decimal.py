"""European "1.234,56" cells must not be read as 1.23456."""

from __future__ import annotations

import pytest

from src.tools.trade_journal_parsers import _to_float


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56", 1234.56),
        ("1.234.567,5", 1234567.5),
        ("€1.234,56", 1234.56),
        ("-1.234,56", -1234.56),
        ("(1.234,56)", -1234.56),
        ("12.345,6 EUR", 12345.6),
    ],
)
def test_comma_decimal_with_dot_grouping(raw: str, expected: float) -> None:
    assert _to_float(raw) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234.56", 1234.56), ("1,234,567.89", 1234567.89), ("1,234", 1234.0), ("12.5", 12.5), ("$1,000", 1000.0)],
)
def test_us_formats_are_unchanged(raw: str, expected: float) -> None:
    assert _to_float(raw) == pytest.approx(expected)
