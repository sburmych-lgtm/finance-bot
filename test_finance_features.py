from datetime import date
from decimal import Decimal

import pytest

from finance_features import (
    advance_recurrence,
    build_financial_insights,
    build_weekly_digest,
    detect_recurring_candidates,
    due_recurrence_dates,
    forecast_month_result,
    recurrence_occurrence_key,
)


def tx(
    day,
    *,
    tx_type="expense",
    amount="100.00",
    category="Оренда",
    description="",
    payment_source=None,
    original_amount=None,
    currency="UAH",
    subcategory=None,
):
    return {
        "date": day.isoformat(),
        "type": tx_type,
        "amount": original_amount if original_amount is not None else amount,
        "currency": currency,
        "amount_uah": amount,
        "category": category,
        "subcategory": subcategory,
        "description": description,
        "payment_source": payment_source,
    }


def test_monthly_recurrence_clamps_to_month_end_without_drifting_anchor():
    assert advance_recurrence(date(2026, 1, 31), "monthly", anchor_day=31) == date(
        2026, 2, 28
    )
    assert advance_recurrence(date(2026, 2, 28), "monthly", anchor_day=31) == date(
        2026, 3, 31
    )
    assert advance_recurrence(date(2024, 1, 31), "monthly", anchor_day=31) == date(
        2024, 2, 29
    )


def test_due_dates_are_bounded_and_resume_after_last_generated_date():
    assert due_recurrence_dates(
        start_date=date(2026, 1, 31),
        through=date(2026, 4, 30),
        frequency="monthly",
        last_generated_date=date(2026, 2, 28),
        anchor_day=31,
    ) == (date(2026, 3, 31), date(2026, 4, 30))

    with pytest.raises(ValueError, match="max_occurrences"):
        due_recurrence_dates(
            start_date=date(2020, 1, 1),
            through=date(2026, 1, 1),
            frequency="daily",
            max_occurrences=10,
        )


def test_occurrence_key_is_stable_and_namespaced():
    assert recurrence_occurrence_key(42, date(2026, 7, 12)) == (
        "recurring:42:2026-07-12"
    )


def test_recurring_detection_requires_three_matching_operations_and_is_stable():
    rows = [
        tx(date(2026, 1, 31), amount="12500", description="Офіс", payment_source="card"),
        tx(date(2026, 2, 28), amount="12500", description="Офіс", payment_source="card"),
        tx(date(2026, 3, 31), amount="12500", description="Офіс", payment_source="card"),
        tx(date(2026, 1, 5), amount="80", category="Кафе"),
        tx(date(2026, 1, 12), amount="80", category="Кафе"),
    ]

    candidates = detect_recurring_candidates(rows)

    assert len(candidates) == 1
    assert candidates[0]["frequency"] == "monthly"
    assert candidates[0]["occurrences"] == 3
    assert candidates[0]["next_date"] == "2026-04-30"
    assert candidates[0]["amount_uah"] == "12500.00"
    assert candidates[0]["payment_source"] == "card"


def test_recurring_detection_does_not_merge_sources_or_income_and_expense():
    rows = [
        tx(date(2026, 1, 1), payment_source="cash"),
        tx(date(2026, 1, 8), payment_source="cash"),
        tx(date(2026, 1, 15), payment_source="card"),
        tx(date(2026, 1, 22), tx_type="income", payment_source="cash"),
    ]

    assert detect_recurring_candidates(rows) == ()


def test_recurring_detection_groups_original_currency_amount_and_subcategory():
    usd_rows = [
        tx(
            date(2026, month, 10),
            amount=uah,
            original_amount="100",
            currency="USD",
            category="Підписки",
            subcategory="Софт",
            description="Service",
        )
        for month, uah in ((1, "4150"), (2, "4175"), (3, "4200"))
    ]
    noise = [
        tx(
            date(2026, month, 10),
            amount="4200",
            original_amount="100",
            currency="EUR",
            category="Підписки",
            subcategory="Софт",
            description="Service",
        )
        for month in (1, 2)
    ] + [
        tx(
            date(2026, month, 10),
            amount="4200",
            original_amount="100",
            currency="USD",
            category="Підписки",
            subcategory="Хостинг",
            description="Service",
        )
        for month in (1, 2)
    ]

    candidates = detect_recurring_candidates([*usd_rows, *noise])

    assert len(candidates) == 1
    assert candidates[0]["amount"] == "100.00"
    assert candidates[0]["currency"] == "USD"
    assert candidates[0]["subcategory"] == "Софт"
    assert candidates[0]["amount_uah"] == "4200.00"


def test_weekly_digest_uses_uah_and_returns_top_expense_category():
    rows = [
        tx(date(2026, 7, 6), amount="300", category="Їжа"),
        tx(date(2026, 7, 7), amount="200", category="Їжа"),
        tx(date(2026, 7, 8), amount="150", category="Таксі"),
        tx(date(2026, 7, 9), tx_type="income", amount="1000", category="Клієнти"),
        tx(date(2026, 7, 5), amount="99999", category="Поза періодом"),
    ]

    digest = build_weekly_digest(rows, week_start=date(2026, 7, 6))

    assert digest == {
        "period_start": "2026-07-06",
        "period_end": "2026-07-12",
        "total_income": "1000.00",
        "total_expense": "650.00",
        "net": "350.00",
        "transaction_count": 4,
        "top_expense_category": "Їжа",
        "top_expense_amount": "500.00",
    }


def test_insights_cover_budget_trend_and_income_concentration_without_ai():
    rows = [
        tx(date(2026, 7, 6), amount="450", category="Кава"),
        tx(date(2026, 7, 7), amount="450", category="Кава"),
        tx(date(2026, 6, 29), amount="300", category="Кава"),
        tx(date(2026, 6, 30), amount="300", category="Кава"),
        tx(date(2026, 7, 8), tx_type="income", amount="800", category="Клієнт A"),
        tx(date(2026, 7, 9), tx_type="income", amount="200", category="Клієнт B"),
    ]
    budgets = [{"category": "Кава", "monthly_limit_uah": "1000"}]

    insights = build_financial_insights(
        rows, budgets=budgets, today=date(2026, 7, 12)
    )
    by_kind = {item["kind"]: item for item in insights}

    assert by_kind["budget_warning"]["percent"] == 90
    assert by_kind["weekly_category_change"]["percent"] == 50
    assert by_kind["income_concentration"]["percent"] == 80
    assert by_kind["income_concentration"]["category"] == "Клієнт A"


def test_insights_omit_division_by_zero_and_low_signal_results():
    rows = [tx(date(2026, 7, 8), amount="10", category="Кава")]
    assert build_financial_insights(rows, budgets=[], today=date(2026, 7, 12)) == ()


def test_forecast_is_explicit_month_result_not_invented_account_balance():
    current = [
        tx(date(2026, 7, 1), tx_type="income", amount="10000"),
        tx(date(2026, 7, 2), amount="2500"),
    ]
    scheduled = [
        {"date": "2026-07-20", "type": "income", "amount_uah": "3000"},
        {"date": "2026-07-25", "type": "expense", "amount_uah": "1500"},
        {"date": "2026-08-01", "type": "expense", "amount_uah": "9999"},
    ]

    result = forecast_month_result(
        current,
        scheduled,
        year=2026,
        month=7,
        estimated_tax_uah=Decimal("1000"),
    )

    assert result == {
        "current_net": "7500.00",
        "scheduled_income": "3000.00",
        "scheduled_expense": "1500.00",
        "estimated_tax": "1000.00",
        "projected_result_before_tax": "9000.00",
        "projected_result_after_tax": "8000.00",
        "basis": "recorded_plus_scheduled",
    }
