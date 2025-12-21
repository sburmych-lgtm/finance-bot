import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import json
from datetime import datetime
import re
from calendar import month_name

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Файл для зберігання транзакцій
DATA_FILE = 'transactions.json'

# Категорії для автоматичного розпізнавання
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

# Українські назви місяців
MONTHS_UK = {
    'січень': 1, 'лютий': 2, 'березень': 3, 'квітень': 4,
    'травень': 5, 'червень': 6, 'липень': 7, 'серпень': 8,
    'вересень': 9, 'жовтень': 10, 'листопад': 11, 'грудень': 12
}

def load_data():
    """Завантажити дані з файлу"""
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_data(data):
    """Зберегти дані у файл"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def parse_transaction(text):
    """Парсити транзакцію з тексту"""
    text_lower = text.lower().strip()
    
    # Визначити тип (дохід/витрата)
    is_income = any(word in text_lower for word in ['зарплата', 'salary', '+', 'дохід', 'отримав'])
    trans_type = 'income' if is_income else 'expense'
    
    # Знайти суму
    amount_match = re.search(r'(\d+(?:[.,]\d{1,2})?)', text)
    if not amount_match:
        return None
    
    amount = float(amount_match.group(1).replace(',', '.'))
    
    # Визначити категорію
    category = 'Інше'
    for cat_name, keywords in CATEGORIES[trans_type].items():
        if any(kw in text_lower for kw in keywords):
            category = cat_name
            break
    
    # Визначити дату (за замовчуванням сьогодні)
    date = datetime.now()
    
    # Перевірити чи є дата в тексті (формат: 15.12 або 15.12.2025)
    date_match = re.search(r'(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?', text)
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        year = int(date_match.group(3)) if date_match.group(3) else datetime.now().year
        try:
            date = datetime(year, month, day)
        except ValueError:
            pass  # Якщо дата неправильна, залишаємо сьогоднішню
    
    return {
        'amount': amount,
        'type': trans_type,
        'category': category,
        'description': text,
        'date': date.strftime('%Y-%m-%d'),
        'timestamp': date.strftime('%Y-%m-%d %H:%M:%S')
    }

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
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
        "/help або /допомога - довідка"
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /balance - показати баланс"""
    user_id = str(update.effective_user.id)
    data = load_data()
    
    if user_id not in data or not data[user_id]:
        await update.message.reply_text("📭 У вас поки немає транзакцій.\n\nНапишіть щось на зразок: 100 кава")
        return
    
    total_income = sum(t['amount'] for t in data[user_id] if t['type'] == 'income')
    total_expense = sum(t['amount'] for t in data[user_id] if t['type'] == 'expense')
    balance = total_income - total_expense
    
    await update.message.reply_text(
        f"💼 Ваш баланс:\n\n"
        f"💰 Доходи: {total_income:.2f} грн\n"
        f"💸 Витрати: {total_expense:.2f} грн\n"
        f"━━━━━━━━━━━━\n"
        f"📊 Баланс: {balance:.2f} грн"
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /history - показати останні 10 транзакцій"""
    user_id = str(update.effective_user.id)
    data = load_data()
    
    if user_id not in data or not data[user_id]:
        await update.message.reply_text("📭 У вас поки немає транзакцій.")
        return
    
    transactions = data[user_id][-15:]  # Останні 15
    
    text = "📜 Останні транзакції:\n\n"
    for t in reversed(transactions):
        emoji = "💸" if t['type'] == 'expense' else "💰"
        text += f"{emoji} {t['amount']:.2f} грн - {t['category']}\n"
        text += f"   📅 {t['date']}\n\n"
    
    await update.message.reply_text(text)

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /report або /звіт - звіт за місяць"""
    user_id = str(update.effective_user.id)
    data = load_data()
    
    if user_id not in data or not data[user_id]:
        await update.message.reply_text("📭 У вас поки немає транзакцій.")
        return
    
    # Визначити місяць (поточний або заданий)
    current_month = datetime.now().month
    current_year = datetime.now().year
    
    # Якщо користувач вказав місяць: /звіт листопад
    if context.args:
        month_name_input = context.args[0].lower()
        if month_name_input in MONTHS_UK:
            current_month = MONTHS_UK[month_name_input]
    
    # Фільтрувати транзакції за місяцем
    month_transactions = [
        t for t in data[user_id]
        if datetime.strptime(t['date'], '%Y-%m-%d').month == current_month
        and datetime.strptime(t['date'], '%Y-%m-%d').year == current_year
    ]
    
    if not month_transactions:
        await update.message.reply_text(f"📭 Немає транзакцій за цей місяць.")
        return
    
    # Підрахунок по категоріях
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
    
    # Формування звіту
    month_names = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                   'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень']
    
    text = f"📊 Звіт за {month_names[current_month]} {current_year}:\n\n"
    text += "💸 Витрати по категоріях:\n"
    
    for cat, amount in sorted(expenses_by_cat.items(), key=lambda x: x[1], reverse=True):
        text += f"  • {cat}: {amount:.2f} грн\n"
    
    text += f"\n━━━━━━━━━━━━\n"
    text += f"💰 Доходи: {total_income:.2f} грн\n"
    text += f"💸 Витрати: {total_expense:.2f} грн\n"
    text += f"📊 Баланс: {total_income - total_expense:.2f} грн"
    
    await update.message.reply_text(text)

async def clear_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /clear або /очистити - очистити всі дані"""
    user_id = str(update.effective_user.id)
    data = load_data()
    
    if user_id in data:
        del data[user_id]
        save_data(data)
        await update.message.reply_text("🗑️ Всі дані очищено!")
    else:
        await update.message.reply_text("📭 У вас і так немає даних.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробка звичайних повідомлень (транзакції)"""
    user_id = str(update.effective_user.id)
    text = update.message.text
    
    # Парсити транзакцію
    transaction = parse_transaction(text)
    
    if not transaction:
        await update.message.reply_text(
            "❌ Не зрозумів. Спробуйте так:\n"
            "• 100 кава\n"
            "• 500 таксі 15.12\n"
            "• зарплата 30000"
        )
        return
    
    # Зберегти
    data = load_data()
    if user_id not in data:
        data[user_id] = []
    
    data[user_id].append(transaction)
    save_data(data)
    
    # Відповідь
    emoji = "💸" if transaction['type'] == 'expense' else "💰"
    await update.message.reply_text(
        f"{emoji} Записано!\n\n"
        f"Сума: {transaction['amount']:.2f} грн\n"
        f"Категорія: {transaction['category']}\n"
        f"Дата: {transaction['date']}\n"
        f"Тип: {'Витрата' if transaction['type'] == 'expense' else 'Дохід'}"
    )

def main():
    """Запуск бота"""
    TOKEN = "8304522900:AAE9C8QXWjwo1BJ0Xwg2Vt5tXMcS3MSpOlk"
    
    application = Application.builder().token(TOKEN).build()
    
    # Команди (українські + англійські)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("баланс", balance))
    application.add_handler(CommandHandler("history", history))
    application.add_handler(CommandHandler("історія", history))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("звіт", report))
    application.add_handler(CommandHandler("clear", clear_data))
    application.add_handler(CommandHandler("очистити", clear_data))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("допомога", start))
    
    # Обробка повідомлень
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Бот запущено!")
    application.run_polling()

if __name__ == '__main__':
    main()