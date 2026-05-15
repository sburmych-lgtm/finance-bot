"""Smoke-тести для parse_transaction нової версії."""
import os
os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'placeholder:test')

import bot


def test_parse_simple_expense():
    t = bot.parse_transaction("100 кава")
    assert t is not None
    assert t['amount'] == 100.0
    assert t['type'] == 'expense'
    assert t['currency'] == 'UAH'
    assert t['category'] == 'Кафе'


def test_parse_income_via_plus():
    t = bot.parse_transaction("+5000 фріланс")
    assert t['type'] == 'income'
    assert t['amount'] == 5000.0


def test_parse_expense_via_minus():
    t = bot.parse_transaction("-200 таксі")
    assert t['type'] == 'expense'
    assert t['amount'] == 200.0
    assert t['category'] == 'Транспорт'


def test_parse_salary_keyword_income():
    t = bot.parse_transaction("зарплата 30000")
    assert t['type'] == 'income'
    assert t['category'] == 'Зарплата'


def test_parse_usd_currency():
    t = bot.parse_transaction("50 usd кава")
    assert t['currency'] == 'USD'
    assert t['category'] == 'Кафе'


def test_parse_eur_currency_symbol():
    t = bot.parse_transaction("25€ обід")
    assert t['currency'] == 'EUR'


def test_parse_with_comma_amount():
    t = bot.parse_transaction("99,50 таксі")
    assert t['amount'] == 99.5


def test_parse_no_amount_returns_none():
    assert bot.parse_transaction("просто слова") is None


def test_parse_unknown_category_falls_to_other():
    t = bot.parse_transaction("777 щось дивне")
    assert t['category'] == 'Інше'


def test_parse_time_input_minutes():
    assert bot.parse_time_input("90") == 90
    assert bot.parse_time_input("45хв") == 45


def test_parse_time_input_hours():
    assert bot.parse_time_input("1.5год") == 90
    assert bot.parse_time_input("2h") == 120


def test_parse_time_input_combined():
    assert bot.parse_time_input("2год 30хв") == 150


def test_parse_time_input_invalid():
    assert bot.parse_time_input("abc") is None
