"""Pure financial feature calculations shared by API jobs and tests.

The functions in this module do not read the database or the clock.  Callers
must pass their user-scoped rows and an explicit date, which keeps scheduled
jobs deterministic and prevents accidental cross-user aggregation.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


MONEY_QUANTUM = Decimal("0.01")
SUPPORTED_FREQUENCIES = frozenset({"daily", "weekly", "monthly", "yearly"})


def _money(value: object) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("invalid money value") from exc
    if not result.is_finite():
        raise ValueError("invalid money value")
    return result.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_text(value: Decimal) -> str:
    return f"{value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}"


def _date(value: object) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid date") from exc


def _month_end(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def advance_recurrence(
    current: date,
    frequency: str,
    *,
    interval: int = 1,
    anchor_day: int | None = None,
) -> date:
    """Return the next due date while preserving month-end anchors."""
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ValueError("unsupported frequency")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError("interval must be a positive integer")

    if frequency == "daily":
        return current + timedelta(days=interval)
    if frequency == "weekly":
        return current + timedelta(days=7 * interval)

    if frequency == "monthly":
        zero_based = current.year * 12 + current.month - 1 + interval
        year, month_index = divmod(zero_based, 12)
        month = month_index + 1
        target_day = anchor_day or current.day
        return date(year, month, min(target_day, _month_end(year, month)))

    target_year = current.year + interval
    target_day = anchor_day or current.day
    return date(
        target_year,
        current.month,
        min(target_day, _month_end(target_year, current.month)),
    )


def due_recurrence_dates(
    *,
    start_date: date,
    through: date,
    frequency: str,
    interval: int = 1,
    last_generated_date: date | None = None,
    anchor_day: int | None = None,
    max_occurrences: int = 366,
) -> tuple[date, ...]:
    """Enumerate due dates inclusively, bounded against runaway catch-up."""
    if max_occurrences < 1:
        raise ValueError("max_occurrences must be positive")
    if frequency not in SUPPORTED_FREQUENCIES:
        raise ValueError("unsupported frequency")
    if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
        raise ValueError("interval must be a positive integer")
    if through < start_date:
        return ()

    anchor = anchor_day or start_date.day
    candidate = start_date
    if last_generated_date is not None and last_generated_date >= start_date:
        if frequency in {"daily", "weekly"}:
            step_days = interval * (7 if frequency == "weekly" else 1)
            jumps = ((last_generated_date - start_date).days // step_days) + 1
            candidate = start_date + timedelta(days=jumps * step_days)
        elif frequency == "monthly":
            elapsed_months = (
                (last_generated_date.year - start_date.year) * 12
                + last_generated_date.month
                - start_date.month
            )
            jumps = max(0, elapsed_months // interval)
            if jumps:
                candidate = advance_recurrence(
                    start_date,
                    frequency,
                    interval=jumps * interval,
                    anchor_day=anchor,
                )
            while candidate <= last_generated_date:
                candidate = advance_recurrence(
                    candidate,
                    frequency,
                    interval=interval,
                    anchor_day=anchor,
                )
        else:
            elapsed_years = last_generated_date.year - start_date.year
            jumps = max(0, elapsed_years // interval)
            if jumps:
                candidate = advance_recurrence(
                    start_date,
                    frequency,
                    interval=jumps * interval,
                    anchor_day=anchor,
                )
            while candidate <= last_generated_date:
                candidate = advance_recurrence(
                    candidate,
                    frequency,
                    interval=interval,
                    anchor_day=anchor,
                )

    result: list[date] = []
    while candidate <= through:
        if len(result) >= max_occurrences:
            raise ValueError("max_occurrences exceeded")
        result.append(candidate)
        candidate = advance_recurrence(
            candidate,
            frequency,
            interval=interval,
            anchor_day=anchor,
        )
    return tuple(result)


def recurrence_occurrence_key(recurring_id: object, due_date: date) -> str:
    """Build the transaction idempotency key for a generated occurrence."""
    identifier = str(recurring_id).strip()
    if not identifier:
        raise ValueError("recurring_id is required")
    return f"recurring:{identifier}:{due_date.isoformat()}"


def _normal_text(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _recurring_group_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return (
        str(row.get("type") or ""),
        str(row.get("category") or ""),
        str(row.get("subcategory") or ""),
        _money(row.get("amount", row.get("amount_uah"))),
        str(row.get("currency") or "UAH").upper(),
        str(row.get("payment_source") or ""),
        _normal_text(row.get("description")),
    )


def _detected_frequency(days: Sequence[int]) -> str | None:
    if days and all(6 <= gap <= 8 for gap in days):
        return "weekly"
    if days and all(27 <= gap <= 32 for gap in days):
        return "monthly"
    return None


def detect_recurring_candidates(
    transactions: Iterable[Mapping[str, object]],
    *,
    min_occurrences: int = 3,
) -> tuple[dict[str, object], ...]:
    """Detect conservative weekly/monthly repetitions from one user's rows."""
    if min_occurrences < 3:
        raise ValueError("min_occurrences must be at least 3")

    grouped: dict[tuple[object, ...], list[tuple[date, Mapping[str, object]]]] = (
        defaultdict(list)
    )
    for row in transactions:
        grouped[_recurring_group_key(row)].append((_date(row.get("date")), row))

    candidates: list[dict[str, object]] = []
    for key, raw_rows in grouped.items():
        rows = sorted(raw_rows, key=lambda item: item[0])
        if len(rows) < min_occurrences:
            continue
        gaps = [(right[0] - left[0]).days for left, right in zip(rows, rows[1:])]
        frequency = _detected_frequency(gaps)
        if frequency is None:
            continue

        latest_date, latest = rows[-1]
        anchor_day = max(item[0].day for item in rows)
        next_date = advance_recurrence(
            latest_date,
            frequency,
            anchor_day=anchor_day,
        )
        candidates.append(
            {
                "type": key[0],
                "category": key[1],
                "subcategory": key[2] or None,
                "amount": _money_text(key[3]),
                "currency": key[4],
                "amount_uah": _money_text(_money(latest.get("amount_uah"))),
                "payment_source": latest.get("payment_source"),
                "description": str(latest.get("description") or ""),
                "frequency": frequency,
                "occurrences": len(rows),
                "last_date": latest_date.isoformat(),
                "next_date": next_date.isoformat(),
            }
        )

    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                str(item["next_date"]),
                str(item["type"]),
                str(item["category"]),
            ),
        )
    )


def build_weekly_digest(
    transactions: Iterable[Mapping[str, object]],
    *,
    week_start: date,
) -> dict[str, object]:
    """Aggregate a Monday-through-Sunday digest from UAH-normalized rows."""
    week_end = week_start + timedelta(days=6)
    rows = tuple(
        row
        for row in transactions
        if week_start <= _date(row.get("date")) <= week_end
    )
    income = sum(
        (_money(row.get("amount_uah")) for row in rows if row.get("type") == "income"),
        Decimal("0"),
    )
    expense = sum(
        (
            _money(row.get("amount_uah"))
            for row in rows
            if row.get("type") == "expense"
        ),
        Decimal("0"),
    )
    by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        if row.get("type") == "expense":
            by_category[str(row.get("category") or "Без категорії")] += _money(
                row.get("amount_uah")
            )
    top_category, top_amount = max(
        by_category.items(),
        key=lambda item: (item[1], item[0]),
        default=(None, Decimal("0")),
    )
    return {
        "period_start": week_start.isoformat(),
        "period_end": week_end.isoformat(),
        "total_income": _money_text(income),
        "total_expense": _money_text(expense),
        "net": _money_text(income - expense),
        "transaction_count": len(rows),
        "top_expense_category": top_category,
        "top_expense_amount": _money_text(top_amount),
    }


def _month_bounds(day: date) -> tuple[date, date]:
    return day.replace(day=1), day.replace(day=_month_end(day.year, day.month))


def build_financial_insights(
    transactions: Iterable[Mapping[str, object]],
    *,
    budgets: Iterable[Mapping[str, object]],
    today: date,
) -> tuple[dict[str, object], ...]:
    """Create conservative rule-based insights; never sends data to AI."""
    rows = tuple(transactions)
    month_start, month_end = _month_bounds(today)
    month_rows = tuple(
        row
        for row in rows
        if month_start <= _date(row.get("date")) <= month_end
    )
    insights: list[dict[str, object]] = []

    month_expenses: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in month_rows:
        if row.get("type") == "expense":
            month_expenses[str(row.get("category") or "Без категорії")] += _money(
                row.get("amount_uah")
            )
    for budget in budgets:
        if budget.get("type", "expense") != "expense":
            continue
        category = str(budget.get("category") or "")
        limit = _money(
            budget.get("monthly_limit_uah", budget.get("limit_uah", 0))
        )
        if not category or limit <= 0:
            continue
        spent = month_expenses.get(category, Decimal("0"))
        percent = int((spent * 100 / limit).quantize(Decimal("1"), ROUND_HALF_UP))
        if percent >= 80:
            insights.append(
                {
                    "kind": "budget_warning",
                    "category": category,
                    "spent_uah": _money_text(spent),
                    "limit_uah": _money_text(limit),
                    "percent": percent,
                }
            )

    current_week_start = today - timedelta(days=today.weekday())
    previous_week_start = current_week_start - timedelta(days=7)
    current_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    previous_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        if row.get("type") != "expense":
            continue
        row_date = _date(row.get("date"))
        category = str(row.get("category") or "Без категорії")
        if current_week_start <= row_date <= today:
            current_by_category[category] += _money(row.get("amount_uah"))
        elif previous_week_start <= row_date < current_week_start:
            previous_by_category[category] += _money(row.get("amount_uah"))
    for category, current in current_by_category.items():
        previous = previous_by_category.get(category, Decimal("0"))
        if previous <= 0 or current < Decimal("100"):
            continue
        percent = int(
            (((current - previous) * 100 / previous).quantize(Decimal("1"), ROUND_HALF_UP))
        )
        if abs(percent) >= 20:
            insights.append(
                {
                    "kind": "weekly_category_change",
                    "category": category,
                    "current_uah": _money_text(current),
                    "previous_uah": _money_text(previous),
                    "percent": percent,
                }
            )

    income_by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in month_rows:
        if row.get("type") == "income":
            income_by_category[str(row.get("category") or "Без категорії")] += _money(
                row.get("amount_uah")
            )
    total_income = sum(income_by_category.values(), Decimal("0"))
    if total_income > 0 and income_by_category:
        category, amount = max(
            income_by_category.items(), key=lambda item: (item[1], item[0])
        )
        percent = int((amount * 100 / total_income).quantize(Decimal("1"), ROUND_HALF_UP))
        if percent >= 40 and total_income >= Decimal("100"):
            insights.append(
                {
                    "kind": "income_concentration",
                    "category": category,
                    "amount_uah": _money_text(amount),
                    "percent": percent,
                }
            )

    priority = {
        "budget_warning": 0,
        "weekly_category_change": 1,
        "income_concentration": 2,
    }
    return tuple(
        sorted(
            insights,
            key=lambda item: (
                priority[str(item["kind"])],
                -abs(int(item.get("percent", 0))),
                str(item.get("category", "")),
            ),
        )
    )


def forecast_month_result(
    recorded_transactions: Iterable[Mapping[str, object]],
    scheduled_occurrences: Iterable[Mapping[str, object]],
    *,
    year: int,
    month: int,
    estimated_tax_uah: Decimal | str | int | float = Decimal("0"),
) -> dict[str, str]:
    """Project the month's result, explicitly not an external account balance."""
    if not 1 <= month <= 12:
        raise ValueError("invalid month")

    def in_month(row: Mapping[str, object]) -> bool:
        row_date = _date(row.get("date"))
        return row_date.year == year and row_date.month == month

    recorded = tuple(row for row in recorded_transactions if in_month(row))
    scheduled = tuple(row for row in scheduled_occurrences if in_month(row))
    recorded_income = sum(
        (
            _money(row.get("amount_uah"))
            for row in recorded
            if row.get("type") == "income"
        ),
        Decimal("0"),
    )
    recorded_expense = sum(
        (
            _money(row.get("amount_uah"))
            for row in recorded
            if row.get("type") == "expense"
        ),
        Decimal("0"),
    )
    scheduled_income = sum(
        (
            _money(row.get("amount_uah"))
            for row in scheduled
            if row.get("type") == "income"
        ),
        Decimal("0"),
    )
    scheduled_expense = sum(
        (
            _money(row.get("amount_uah"))
            for row in scheduled
            if row.get("type") == "expense"
        ),
        Decimal("0"),
    )
    current_net = recorded_income - recorded_expense
    before_tax = current_net + scheduled_income - scheduled_expense
    tax = _money(estimated_tax_uah)
    return {
        "current_net": _money_text(current_net),
        "scheduled_income": _money_text(scheduled_income),
        "scheduled_expense": _money_text(scheduled_expense),
        "estimated_tax": _money_text(tax),
        "projected_result_before_tax": _money_text(before_tax),
        "projected_result_after_tax": _money_text(before_tax - tax),
        "basis": "recorded_plus_scheduled",
    }
