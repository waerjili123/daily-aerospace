"""Precision-aware deadline comparisons shared by planning and reporting."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

DeadlinePrecision = Literal["date", "minute", "second"]


def deadline_is_expired(
    deadline: datetime, precision: DeadlinePrecision, now: datetime
) -> bool:
    if precision == "date":
        return now.astimezone(deadline.tzinfo).date() > deadline.date()
    return now.astimezone(deadline.tzinfo) > deadline


def deadline_is_current(
    deadline: datetime, precision: DeadlinePrecision, now: datetime
) -> bool:
    return not deadline_is_expired(deadline, precision, now)
