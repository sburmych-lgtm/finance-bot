"""Regression contracts for Telegram Markdown and message-size safety."""

from datetime import datetime
from types import SimpleNamespace

import bot
from test_feature_matrix import context, run, use_database


DANGEROUS_NAME = "`[ops]_team*"
DANGEROUS_EMOJI = "`[icon]_*"


class UnsafeTelegramMarkdown(RuntimeError):
    pass


class TelegramMessageTooLong(RuntimeError):
    pass


def validate_telegram_send(text, kwargs, dangerous_values=()):
    if len(text) > 4096:
        raise TelegramMessageTooLong(f"Telegram text has {len(text)} characters")
    parse_mode = str(kwargs.get("parse_mode") or "").lower()
    if parse_mode.startswith("markdown"):
        for value in dangerous_values:
            if value and value in text:
                raise UnsafeTelegramMarkdown(
                    f"unescaped user value in Telegram Markdown: {value!r}"
                )


class StrictMessage:
    def __init__(self, text="", *, dangerous_values=()):
        self.text = text
        self.dangerous_values = tuple(dangerous_values)
        self.replies = []

    async def reply_text(self, text, **kwargs):
        validate_telegram_send(text, kwargs, self.dangerous_values)
        self.replies.append({"text": text, "kwargs": kwargs})


class StrictQuery:
    def __init__(self, data, user_id=101, *, dangerous_values=()):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = StrictMessage(dangerous_values=dangerous_values)
        self.dangerous_values = tuple(dangerous_values)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, "kwargs": kwargs})

    async def edit_message_text(self, text, **kwargs):
        validate_telegram_send(text, kwargs, self.dangerous_values)
        self.edits.append({"text": text, "kwargs": kwargs})


def strict_update(
    *, user_id=101, text="", callback_data=None, dangerous_values=(), update_id=7001
):
    user = SimpleNamespace(
        id=user_id,
        username="markdown_user",
        first_name="Ruby",
        last_name="Tester",
        language_code="uk",
    )
    query = (
        StrictQuery(callback_data, user_id, dangerous_values=dangerous_values)
        if callback_data is not None
        else None
    )
    message = StrictMessage(text, dangerous_values=dangerous_values)
    return SimpleNamespace(
        update_id=update_id,
        effective_user=user,
        message=None if query else message,
        callback_query=query,
    )


def retry_once_after_markdown_failure(call):
    try:
        run(call())
    except UnsafeTelegramMarkdown:
        try:
            run(call())
        except UnsafeTelegramMarkdown:
            pass


def test_time_category_selection_escapes_user_markdown(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["time_categories"].update({
            DANGEROUS_NAME: {"emoji": "⏱️"}
        }),
    ))
    update = strict_update(
        callback_data=f"timecat:{DANGEROUS_NAME}",
        dangerous_values=(DANGEROUS_NAME,),
    )
    ctx = context()

    run(bot.handle_callback(update, ctx))

    assert ctx.user_data["waiting_for"] == f"time_minutes:{DANGEROUS_NAME}"
    assert update.callback_query.edits


def test_undo_preview_escapes_historical_category_markdown(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(database.add_transaction(
        "101", 10, "UAH", 10, "expense", DANGEROUS_NAME, "",
        "2026-07-13", "2026-07-13 10:00:00",
    ))
    update = strict_update(
        callback_data="undo:last", dangerous_values=(DANGEROUS_NAME,)
    )

    run(bot.handle_callback(update, context()))

    assert update.callback_query.edits


def test_employee_settings_escape_user_markdown(monkeypatch, tmp_path):
    use_database(monkeypatch, tmp_path)
    run(bot.update_user_settings(
        "101", lambda settings: settings["employees"].append(DANGEROUS_NAME)
    ))
    update = strict_update(
        callback_data="settings:employees", dangerous_values=(DANGEROUS_NAME,)
    )

    run(bot.handle_callback(update, context()))

    assert update.callback_query.edits


def test_numpad_save_escapes_user_emoji_markdown(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    category = "Картка"
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["categories"]["expense"].update({
            category: {
                "emoji": DANGEROUS_EMOJI,
                "keywords": [],
                "subcategories": [],
            }
        }),
    ))
    update = strict_update(
        callback_data="num:expense:unused:UAH:confirm",
        dangerous_values=(DANGEROUS_EMOJI,),
    )

    run(bot.save_transaction(
        update, context(), "expense", category, "UAH", "25"
    ))

    assert len(run(database.get_transactions("101"))) == 1
    assert update.callback_query.edits


def test_time_save_escapes_user_emoji_markdown(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    category = "Глибока робота"
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["time_categories"].update({
            category: {"emoji": DANGEROUS_EMOJI}
        }),
    ))
    update = strict_update(
        text="90", dangerous_values=(DANGEROUS_EMOJI,)
    )

    run(bot.handle_text_transaction(
        update, context(waiting_for=f"time_minutes:{category}")
    ))

    assert len(run(database.get_time_tracks("101"))) == 1
    assert update.message.replies


def test_free_text_save_escapes_category_markdown(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["categories"]["expense"].update({
            DANGEROUS_NAME: {
                "emoji": "📦",
                "keywords": ["маркер"],
                "subcategories": [],
            }
        }),
    ))
    update = strict_update(
        text="30 маркер", dangerous_values=(DANGEROUS_NAME,)
    )

    run(bot.handle_text_transaction(update, context()))

    assert len(run(database.get_transactions("101"))) == 1
    assert update.message.replies


def test_numpad_markdown_reply_failure_does_not_duplicate_save(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["categories"]["expense"].update({
            DANGEROUS_NAME: {
                "emoji": "📦", "keywords": [], "subcategories": []
            }
        }),
    ))
    update = strict_update(
        callback_data="num:expense:unused:UAH:confirm",
        dangerous_values=(DANGEROUS_NAME,),
    )

    retry_once_after_markdown_failure(lambda: bot.save_transaction(
        update, context(), "expense", DANGEROUS_NAME, "UAH", "15"
    ))

    assert len(run(database.get_transactions("101"))) == 1


def test_time_markdown_reply_failure_does_not_create_retry_transaction(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    category = "Фокус"
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["time_categories"].update({
            category: {"emoji": DANGEROUS_EMOJI}
        }),
    ))
    update = strict_update(text="90", dangerous_values=(DANGEROUS_EMOJI,))
    ctx = context(waiting_for=f"time_minutes:{category}")

    retry_once_after_markdown_failure(
        lambda: bot.handle_text_transaction(update, ctx)
    )

    assert len(run(database.get_time_tracks("101"))) == 1
    assert run(database.get_transactions("101")) == []


def test_free_text_markdown_reply_failure_does_not_duplicate_save(
    monkeypatch, tmp_path
):
    database = use_database(monkeypatch, tmp_path)
    run(bot.update_user_settings(
        "101",
        lambda settings: settings["categories"]["expense"].update({
            DANGEROUS_NAME: {
                "emoji": "📦",
                "keywords": ["маркер"],
                "subcategories": [],
            }
        }),
    ))
    update = strict_update(
        text="45 маркер", dangerous_values=(DANGEROUS_NAME,)
    )

    retry_once_after_markdown_failure(
        lambda: bot.handle_text_transaction(update, context())
    )

    assert len(run(database.get_transactions("101"))) == 1


def test_monthly_report_never_exceeds_telegram_message_limit(monkeypatch, tmp_path):
    database = use_database(monkeypatch, tmp_path)
    year = datetime.now(bot.KYIV_TZ).year
    month = datetime.now(bot.KYIV_TZ).month

    async def seed():
        for index in range(90):
            category = f"Категорія {index:03d} " + "я" * 58
            await database.add_transaction(
                "101", 1, "UAH", 1, "expense", category, "",
                f"{year:04d}-{month:02d}-13",
                f"{year:04d}-{month:02d}-13 10:00:00",
            )

    run(seed())
    update = strict_update(callback_data="report:current")

    run(bot.show_monthly_report(update, context(), year, month))

    sent = update.callback_query.edits + update.callback_query.message.replies
    assert sent
    assert all(len(item["text"]) <= 4096 for item in sent)
