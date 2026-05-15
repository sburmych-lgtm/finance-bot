import logging
import os
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import json
from datetime import datetime
import re

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATA_FILE = os.environ.get('DATA_FILE', 'transactions.json')

CATEGORIES = {
    'expense': {
        'Продукти': ['продукти', 'магазин', 'супермаркет', 'silpo', 'атб', 'groceries', 'їжа'],
        'Кафе': ['кава', 'кафе', 'coffee', 'ресторан', 'обід', 'lunch'],
        'Транспорт': ['таксі', 'taxi', 'uber', 'bolt', 'метро', 'автобус', 'бензин'],
        'Розваги': ['кіно', 'бар', 'клуб', 'пиво', 'cinema'],
        'Здоров\'я': ['аптека', 'лікар', 'ліки', 'pharmacy'],
        'Інше': []
    },
    'income': {
        'Зарплата': ['зарплата', 'salary', 'зп'],
        'Фріланс': ['freelance', 'фріланс', 'проект'],
        'Інше': []
    }
}

MONTHS_UK = {
    'січень': 1, 'лютий': 2, 'березень': 3, 'квітень': 4,
    'травень': 5, 'червень': 6, 'липень': 7, 'серпень': 8,
    'вересень': 9, 'жовтень': 10, 'листопад': 11, 'грудень': 12
}


def load_data():
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _income_markers():
    markers = {'+', 'дохід', 'отримав'}
    for kws in CATEGORIES['income'].values():
        markers.update(kws)
    return markers


def parse_transaction(text):
    text_lower = text.lower().strip()

    is_income = any(word in text_lower for word in _income_markers())
    trans_type = 'income' if is_income else 'expense'

    amount_match = re.search(r'(\d+(?:[.,]\d{1,2})?)', text)
    if not amount_match:
        return None

    amount = float(amount_match.group(1).replace(',', '.'))

    category = 'Інше'
    for cat_name, keywords in CATEGORIES[trans_type].items():
        if any(kw in text_lower for kw in keywords):
            category = cat_name
            break

    date = datetime.now()

    date_match = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?', text)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = int(date_match.group(3)) if date_match.group(3) else datetime.now().year
        try:
            date = datetime(year, month, day)
        except ValueError:
            pass

    return {
        'amount': amount,
        'type': trans_type,
        'category': category,
        'description': text,
        'date': date.strftime('%Y-%m-%d'),
        'timestamp': date.strftime('%Y-%m-%d %H:%M:%S')
    }


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я бот для обліку фінансів.\n\n"
        "📝 Просто пишіть витрати:\n"
        "• 100 кава\n"
        "• 500 таксі 15.12\n"
        "• зарплата 30000\n\n"
        "📊 Команди:\n"
        "/balance або /баланс - баланс\n"
        "/history або /історія - історія\n"
        "/report або /звіт - звіт за місяць\n"
        "/clear або /очистити - очистити дані\n"
        "/myid - дізнатись свій Telegram ID\n"
        "/help або /допомога - довідка"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Ваш Telegram ID: `{update.effective_user.id}`", parse_mode='Markdown')


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data or not data[user_id]:
        await update.message.reply_text("📭 У вас поки немає транзакцій.\n\nНапишіть щось на зразок: 100 кава")
        return

    total_income = sum(t['amount'] for t in data[user_id] if t['type'] == 'income')
    total_expense = sum(t['amount'] for t in data[user_id] if t['type'] == 'expense')
    bal = total_income - total_expense

    await update.message.reply_text(
        f"💼 Ваш баланс:\n\n"
        f"💰 Доходи: {total_income:.2f} грн\n"
        f"💸 Витрати: {total_expense:.2f} грн\n"
        f"━━━━━━━━━━━━\n"
        f"📊 Баланс: {bal:.2f} грн"
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data or not data[user_id]:
        await update.message.reply_text("📭 У вас поки немає транзакцій.")
        return

    transactions = data[user_id][-15:]

    text = "📜 Останні транзакції:\n\n"
    for t in reversed(transactions):
        emoji = "💸" if t['type'] == 'expense' else "💰"
        text += f"{emoji} {t['amount']:.2f} грн - {t['category']}\n"
        text += f"   📅 {t['date']}\n\n"

    await update.message.reply_text(text)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id not in data or not data[user_id]:
        await update.message.reply_text("📭 У вас поки немає транзакцій.")
        return

    current_month = datetime.now().month
    current_year = datetime.now().year

    if context.args:
        month_name_input = context.args[0].lower()
        if month_name_input in MONTHS_UK:
            current_month = MONTHS_UK[month_name_input]

    month_transactions = [
        t for t in data[user_id]
        if datetime.strptime(t['date'], '%Y-%m-%d').month == current_month
        and datetime.strptime(t['date'], '%Y-%m-%d').year == current_year
    ]

    if not month_transactions:
        await update.message.reply_text("📭 Немає транзакцій за цей місяць.")
        return

    expenses_by_cat = {}
    total_expense = 0
    total_income = 0

    for t in month_transactions:
        if t['type'] == 'expense':
            total_expense += t['amount']
            cat = t['category']
            expenses_by_cat[cat] = expenses_by_cat.get(cat, 0) + t['amount']
        else:
            total_income += t['amount']

    month_names = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                   'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень']

    text = f"📊 Звіт за {month_names[current_month]} {current_year}:\n\n"
    text += "💸 Витрати по категоріях:\n"

    for cat, amount in sorted(expenses_by_cat.items(), key=lambda x: x[1], reverse=True):
        text += f"  • {cat}: {amount:.2f} грн\n"

    text += "\n━━━━━━━━━━━━\n"
    text += f"💰 Доходи: {total_income:.2f} грн\n"
    text += f"💸 Витрати: {total_expense:.2f} грн\n"
    text += f"📊 Баланс: {total_income - total_expense:.2f} грн"

    await update.message.reply_text(text)


async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()

    if user_id in data:
        del data[user_id]
        save_data(data)
        await update.message.reply_text("🗑️ Всі дані очищено!")
    else:
        await update.message.reply_text("📭 У вас і так немає даних.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = os.environ.get('ADMIN_ID', '').strip()
    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.message.reply_text("⛔ Ця команда доступна тільки адміністратору.")
        return

    if not context.args:
        await update.message.reply_text(
            "Використання: /broadcast <текст повідомлення>\n\n"
            "Приклад: /broadcast 🛠 Бот оновлено, нові функції доступні."
        )
        return

    message_text = update.message.text.split(maxsplit=1)[1] if len(update.message.text.split(maxsplit=1)) > 1 else ""
    if not message_text:
        await update.message.reply_text("❌ Порожнє повідомлення.")
        return

    data = load_data()
    user_ids = list(data.keys())

    if not user_ids:
        await update.message.reply_text("📭 Поки що немає користувачів для розсилки.")
        return

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=int(uid), text=message_text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            failed += 1
            logger.warning(f"broadcast failed for {uid}: {e}")

    await update.message.reply_text(f"📣 Розсилка завершена. Надіслано: {sent}, помилок: {failed}.")


CYRILLIC_COMMAND_ROUTES = {}  # filled in main() to avoid forward refs


async def cyrillic_command_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or '').strip()
    parts = text.split()
    cmd = parts[0].lower() if parts else ''
    handler = CYRILLIC_COMMAND_ROUTES.get(cmd)
    if not handler:
        return
    context.args = parts[1:]
    await handler(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text or ''

    if text.startswith('/'):
        return

    transaction = parse_transaction(text)

    if not transaction:
        await update.message.reply_text(
            "❌ Не зрозумів. Спробуйте так:\n"
            "• 100 кава\n"
            "• 500 таксі 15.12\n"
            "• зарплата 30000"
        )
        return

    data = load_data()
    if user_id not in data:
        data[user_id] = []

    data[user_id].append(transaction)
    save_data(data)

    emoji = "💸" if transaction['type'] == 'expense' else "💰"
    await update.message.reply_text(
        f"{emoji} Записано!\n\n"
        f"Сума: {transaction['amount']:.2f} грн\n"
        f"Категорія: {transaction['category']}\n"
        f"Дата: {transaction['date']}\n"
        f"Тип: {'Витрата' if transaction['type'] == 'expense' else 'Дохід'}"
    )


async def notify_admin_on_start(application: Application):
    admin_id = os.environ.get('ADMIN_ID', '').strip()
    if not admin_id:
        return
    try:
        await application.bot.send_message(
            chat_id=int(admin_id),
            text="✅ Бот запущено та працює.\n"
                 "Версія: фінансовий бот @Olesia_money_bot\n"
                 "Хостинг: Railway\n"
                 "Команди для адміна: /broadcast <текст>"
        )
    except Exception as e:
        logger.warning(f"admin notify failed: {e}")


def main():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            token = os.environ.get('BOT_TOKEN')
        except ImportError:
            pass

    if not token:
        raise RuntimeError("BOT_TOKEN env variable is not set")

    application = Application.builder().token(token).post_init(notify_admin_on_start).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("clear", clear_data))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("myid", my_id))
    application.add_handler(CommandHandler("broadcast", broadcast))

    CYRILLIC_COMMAND_ROUTES.update({
        '/баланс': balance,
        '/історія': history,
        '/звіт': report,
        '/очистити': clear_data,
        '/допомога': start,
    })
    cyrillic_pattern = r'^/(баланс|історія|звіт|очистити|допомога)(\s|$)'
    application.add_handler(MessageHandler(filters.Regex(cyrillic_pattern), cyrillic_command_dispatch))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущено!")
    application.run_polling()


if __name__ == '__main__':
    main()
