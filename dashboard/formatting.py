"""Presentation helpers for financial values and incomplete data."""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd


def is_missing(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def format_inr(value: Any) -> str:
    """Format a numeric value with Indian digit grouping; missing stays explicit."""
    if is_missing(value):
        return "N/A"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "N/A"
    except (InvalidOperation, ValueError, TypeError):
        return "N/A"
    sign = "-" if amount < 0 else ""
    fixed = f"{abs(amount):,.2f}"
    integer_part, decimal_part = fixed.split(".")
    digits = integer_part.replace(",", "")
    if len(digits) > 3:
        tail = digits[-3:]
        head = digits[:-3]
        groups: list[str] = []
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        integer_part = ",".join(groups + [tail])
    else:
        integer_part = digits
    return f"{sign}₹{integer_part}.{decimal_part}"


def format_pct(value: Any) -> str:
    if is_missing(value):
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return "N/A" if not math.isfinite(number) else f"{number:,.2f}%"


def numeric_or_none(value: Any) -> float | None:
    if is_missing(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None
