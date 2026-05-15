"""Unit-тести для bot.py — перевіряємо парсер транзакцій і збереження даних."""
import json
import os
import tempfile
from datetime import datetime

import bot


def test_parse_simple_expense():
    t = bot.parse_transaction("100 кава")
    assert t is not None
    assert t['amount'] == 100.0
    assert t['type'] == 'expense'
    assert t['category'] == 'Кафе'


def test_parse_groceries():
    t = bot.parse_transaction("250 продукти")
    assert t['category'] == 'Продукти'
    assert t['type'] == 'expense'


def test_parse_income_salary():
    t = bot.parse_transaction("зарплата 30000")
    assert t['type'] == 'income'
    assert t['amount'] == 30000.0
    assert t['category'] == 'Зарплата'


def test_parse_with_dot_amount():
    t = bot.parse_transaction("99.50 таксі")
    assert t['amount'] == 99.5
    assert t['category'] == 'Транспорт'


def test_parse_with_comma_amount():
    t = bot.parse_transaction("99,50 таксі")
    assert t['amount'] == 99.5


def test_parse_with_date():
    t = bot.parse_transaction("500 кіно 15.12.2025")
    assert t['date'] == '2025-12-15'
    assert t['category'] == 'Розваги'


def test_parse_with_short_date():
    t = bot.parse_transaction("300 аптека 01.03")
    year = datetime.now().year
    assert t['date'] == f'{year}-03-01'
    assert t['category'] == "Здоров'я"


def test_parse_invalid_date_fallback_to_today():
    t = bot.parse_transaction("100 кава 99.99")
    assert t['date'] == datetime.now().strftime('%Y-%m-%d')


def test_parse_no_amount_returns_none():
    assert bot.parse_transaction("просто слова") is None


def test_parse_unknown_category_falls_to_other():
    t = bot.parse_transaction("777 щось дивне")
    assert t['category'] == 'Інше'
    assert t['type'] == 'expense'


def test_parse_freelance_income():
    t = bot.parse_transaction("фріланс 5000")
    assert t['type'] == 'income'
    assert t['category'] == 'Фріланс'


def test_parse_plus_marker_makes_income():
    t = bot.parse_transaction("+1000 подарунок")
    assert t['type'] == 'income'


def test_load_save_round_trip(tmp_path, monkeypatch):
    f = tmp_path / "tx.json"
    monkeypatch.setattr(bot, "DATA_FILE", str(f))
    bot.save_data({"123": [{"amount": 10, "type": "expense", "category": "Інше",
                              "description": "test", "date": "2026-01-01",
                              "timestamp": "2026-01-01 00:00:00"}]})
    loaded = bot.load_data()
    assert "123" in loaded
    assert loaded["123"][0]['amount'] == 10


def test_load_missing_file_returns_empty(tmp_path, monkeypatch):
    f = tmp_path / "nope.json"
    monkeypatch.setattr(bot, "DATA_FILE", str(f))
    assert bot.load_data() == {}
