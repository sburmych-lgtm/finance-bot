"""Smoke-тести для parse_transaction нової версії."""
import os
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

os.environ.setdefault('TELEGRAM_BOT_TOKEN', 'placeholder:test')

import bot


def _signed_init_data(auth_date):
    token = os.environ['TELEGRAM_BOT_TOKEN']
    params = {
        'auth_date': str(auth_date),
        'query_id': 'test-query',
        'user': json.dumps({'id': 123}, separators=(',', ':')),
    }
    data_check_string = '\n'.join(f'{key}={value}' for key, value in sorted(params.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    params['hash'] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(params)


def test_validate_init_data_rejects_future_auth_date():
    raw = _signed_init_data(int(time.time()) + 3600)
    assert bot.validate_init_data(raw, os.environ['TELEGRAM_BOT_TOKEN']) is None


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
