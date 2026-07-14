"""Contracts for the CSV bank-statement import parser (Block 4)."""
import bot


def test_amount_formats():
    assert bot._parse_import_amount("1 234,56") == 1234.56
    assert bot._parse_import_amount("-500.00") == -500.0
    assert bot._parse_import_amount("1,234.56") == 1234.56
    assert bot._parse_import_amount("300,00") == 300.0
    assert bot._parse_import_amount("+30000") == 30000.0
    assert bot._parse_import_amount("") is None
    assert bot._parse_import_amount("abc") is None


def test_date_formats():
    assert bot._parse_import_date("13.07.2026") == "2026-07-13"
    assert bot._parse_import_date("2026-07-13 10:00:00") == "2026-07-13"
    assert bot._parse_import_date("13/07/2026") == "2026-07-13"
    assert bot._parse_import_date("не дата") is None


def test_csv_amount_with_sign():
    csv = ("Дата,Сума,Валюта,Опис\n"
           "13.07.2026,-250.50,UAH,Кава\n"
           "12.07.2026,+30000,UAH,Зарплата\n")
    rows, errors = bot.parse_import_csv(csv)
    assert errors == []
    assert rows[0] == {"date": "2026-07-13", "type": "expense", "amount": 250.5,
                       "currency": "UAH", "description": "Кава", "line": 1}
    assert rows[1]["type"] == "income" and rows[1]["amount"] == 30000.0


def test_csv_debit_credit_semicolon():
    csv = ("Date;Debit;Credit;Description\n"
           "13.07.2026;250,00;;Продукти\n"
           "12.07.2026;;5000,00;Дохід\n")
    rows, errors = bot.parse_import_csv(csv)
    assert errors == []
    assert rows[0]["type"] == "expense" and rows[0]["amount"] == 250.0
    assert rows[1]["type"] == "income" and rows[1]["amount"] == 5000.0


def test_csv_bad_row_is_reported_not_dropped_silently():
    csv = "Дата,Сума,Опис\n13.07.2026,100,ok\nсміття,,\n"
    rows, errors = bot.parse_import_csv(csv)
    assert len(rows) == 1
    assert len(errors) == 1


def test_csv_empty():
    rows, errors = bot.parse_import_csv("")
    assert rows == [] and errors


def test_pdf_bad_input_is_graceful():
    # non-PDF bytes must never crash — returns ([], [error])
    rows, errors = bot.parse_import_pdf(b'this is not a pdf')
    assert rows == [] and errors


def test_pdf_empty_is_graceful():
    rows, errors = bot.parse_import_pdf(b'')
    assert rows == [] and errors
