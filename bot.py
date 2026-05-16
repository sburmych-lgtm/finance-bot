import logging
import os
import sqlite3
import json
import hmac
import hashlib
from urllib.parse import parse_qsl, unquote
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import re
from collections import defaultdict
import asyncio
import aiohttp
from aiohttp import web

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ========== TELEGRAM INIT DATA VALIDATION ==========
def validate_init_data(raw_init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram Mini App initData per official HMAC-SHA256 spec.

    Returns parsed dict (with user already JSON-decoded) on success, None on failure.
    """
    try:
        params = dict(parse_qsl(raw_init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = params.pop('hash', None)
    if not received_hash:
        return None

    # Check auth_date freshness (24 h window)
    auth_date_str = params.get('auth_date', '')
    try:
        auth_ts = int(auth_date_str)
        if datetime.now(timezone.utc).timestamp() - auth_ts > 86400:
            return None
    except (ValueError, TypeError):
        return None

    # Build data-check-string: sorted key=value pairs joined by \n
    data_check_string = '\n'.join(
        f'{k}={v}' for k, v in sorted(params.items())
    )

    secret_key = hmac.new(
        b'WebAppData',
        bot_token.encode(),
        hashlib.sha256
    ).digest()

    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    # Decode user JSON if present
    if 'user' in params:
        try:
            params['user'] = json.loads(params['user'])
        except (json.JSONDecodeError, TypeError):
            pass

    return params


# Configuration
KYIV_TZ = ZoneInfo("Europe/Kyiv")
DATA_DIR = os.environ.get('DATA_DIR', '.')
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.environ.get('DB_FILE', os.path.join(DATA_DIR, 'finance.db'))
SETTINGS_FILE = os.environ.get('SETTINGS_FILE', os.path.join(DATA_DIR, 'settings.json'))
ADMIN_IDS = {x.strip() for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()}

# Lock for database operations
db_lock = asyncio.Lock()


def is_admin(user_id) -> bool:
    return str(user_id) in ADMIN_IDS


async def has_access(user_id) -> bool:
    """Зараз тримаємо бот у FREE-режимі для всіх. Інфраструктура для майбутнього paywall —
    адмін без обмежень; пейволу нема, тому решта також пропускається. Коли увімкнемо
    монетизацію — змінимо False default + перевірка expires_at."""
    if is_admin(user_id):
        return True
    return True  # FREE for everyone until monetization is turned on

# Exchange rate cache
exchange_rates_cache = {
    'USD': 41.5,
    'EUR': 45.2,
    'last_update': None
}

# Default settings
DEFAULT_SETTINGS = {
    'employees': ['Катя', 'Ілона', 'Мирослав', 'Христина', 'Інші'],
    'tax_config': {
        'single_tax_rate': 0.05,
        'esv_fixed': 1760,
        'note': 'Для ФОП 3 група: 5% + фіксований ЄСВ'
    },
    'categories': {
        'expense': {
            'Продукти': {'emoji': '🛒', 'keywords': ['продукти', 'магазин', 'супермаркет', 'silpo', 'атб', 'groceries', 'їжа']},
            'Кафе': {'emoji': '☕', 'keywords': ['кава', 'кафе', 'coffee', 'ресторан', 'обід', 'lunch', 'латте', 'капучино', 'еспресо']},
            'Транспорт': {'emoji': '🚕', 'keywords': ['таксі', 'taxi', 'uber', 'bolt', 'метро', 'автобус', 'бензин', 'укрзалізниця', 'поїздка', 'проїзд']},
            'Розваги': {'emoji': '🎭', 'keywords': ['кіно', 'бар', 'клуб', 'пиво', 'cinema']},
            'Здоров\'я': {'emoji': '💊', 'keywords': ['аптека', 'лікар', 'pharmacy', 'hospital']},
            'Ліки': {'emoji': '💊', 'keywords': ['ліки', 'medicine', 'таблетки', 'препарат', 'анальгін']},
            'Подарунки': {'emoji': '🎁', 'keywords': ['подарунки', 'подарок', 'gift', 'present']},
            'Податки': {'emoji': '📋', 'keywords': ['податки', 'tax', 'taxes', 'пдв', 'єдиний податок']},
            'Косметолог': {'emoji': '💄', 'keywords': ['косметолог', 'cosmetologist', 'beautician']},
            'Салон краси': {'emoji': '💅', 'keywords': ['салон', 'перукар', 'beauty salon', 'manicure', 'манікюр']},
            'Догляд/Косметика': {'emoji': '🧴', 'keywords': ['косметика', 'skincare', 'догляд', 'крем']},
            'Вітаміни': {'emoji': '💊', 'keywords': ['вітаміни', 'vitamins', 'supplements', 'добавки']},
            'Одяг': {'emoji': '👗', 'keywords': ['одяг', 'clothes', 'clothing', 'взуття']},
            'Комунальні': {'emoji': '🏠', 'keywords': ['комунальні', 'комуналка', 'світло', 'вода', 'газ', 'опалення', 'utilities']},
            'Інше': {'emoji': '📦', 'keywords': []}
        },
        'income': {
            'Зарплата': {'emoji': '💰', 'keywords': ['зарплата', 'salary', 'зп']},
            'Фріланс': {'emoji': '💼', 'keywords': ['freelance', 'фріланс', 'проект']},
            'Консультації': {'emoji': '⚖️', 'keywords': ['консультація', 'consultation']},
            'ВЛК': {'emoji': '🏥', 'keywords': ['влк', 'військово-лікарська комісія']},
            'ТЦК': {'emoji': '🪖', 'keywords': ['тцк', 'territorial recruitment']},
            'Суди': {'emoji': '🏛️', 'keywords': ['суд', 'судові', 'court']},
            'Інше': {'emoji': '📦', 'keywords': []}
        }
    },
    'time_categories': {
        'Сон': {'emoji': '😴'},
        'Робота': {'emoji': '💼'},
        'Зал': {'emoji': '🏋️'},
        'Їжа': {'emoji': '🍽️'},
        'Терапія': {'emoji': '🧘'},
        'Відпустка': {'emoji': '🏖️'},
        'Уроки історії': {'emoji': '📚'},
        'Уроки англійської': {'emoji': '🇬🇧'},
        'Підвищення кваліфікації': {'emoji': '📈'},
        'Навчання': {'emoji': '🎓'},
        'Батьки': {'emoji': '👨‍👩‍👧'},
        'Стосунки': {'emoji': '💕'},
        'Друзі': {'emoji': '👯'},
        'Безкоштовна робота': {'emoji': '🎁'},
        'Шопінг': {'emoji': '🛍️'},
        'Розваги': {'emoji': '🎉'},
        'Скрол стрічки': {'emoji': '📱'},
        'Інше': {'emoji': '📦'}
    }
}

MONTH_NAMES = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
               'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень']


# ========== DATABASE CLASS ==========
class Database:
    """Thread-safe SQLite database wrapper"""

    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        self.conn = None
        self.init_db()

    def init_db(self):
        """Initialize database and create tables"""
        self.conn = sqlite3.connect(self.db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()

        # Transactions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT DEFAULT 'UAH',
                amount_uah REAL NOT NULL,
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT,
                date DATE NOT NULL,
                timestamp DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_date
            ON transactions(user_id, date)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_user_type
            ON transactions(user_id, type)
        ''')

        # Time tracks table (NEW!)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS time_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                category TEXT NOT NULL,
                description TEXT,
                date DATE NOT NULL,
                timestamp DATETIME NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_time_user_date
            ON time_tracks(user_id, date)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id TEXT PRIMARY KEY,
                plan TEXT NOT NULL DEFAULT 'free',
                expires_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                language_code TEXT,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.conn.commit()
        logger.info("Database initialized successfully")

    async def upsert_user(self, user):
        if not user:
            return
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name, last_name, language_code)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    last_name=excluded.last_name,
                    language_code=excluded.language_code,
                    last_seen=CURRENT_TIMESTAMP
            ''', (str(user.id), user.username, user.first_name, user.last_name, user.language_code))
            self.conn.commit()

    async def get_all_user_ids(self):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            return [row[0] for row in cursor.fetchall()]

    async def get_subscription(self, user_id):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM subscriptions WHERE user_id = ?", (str(user_id),))
            row = cursor.fetchone()
            return dict(row) if row else None

    async def set_subscription(self, user_id, plan, expires_at=None):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO subscriptions (user_id, plan, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    plan=excluded.plan,
                    expires_at=excluded.expires_at,
                    updated_at=CURRENT_TIMESTAMP
            ''', (str(user_id), plan, expires_at))
            self.conn.commit()

    async def add_transaction(self, user_id, amount, currency, amount_uah, t_type,
                             category, description, date, timestamp):
        """Add transaction to database"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO transactions
                (user_id, amount, currency, amount_uah, type, category, description, date, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, amount, currency, amount_uah, t_type, category, description, date, timestamp))
            self.conn.commit()
            return cursor.lastrowid

    async def add_time_track(self, user_id, minutes, category, description, date, timestamp):
        """Add time track to database"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO time_tracks
                (user_id, minutes, category, description, date, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, minutes, category, description, date, timestamp))
            self.conn.commit()
            return cursor.lastrowid

    async def get_transactions(self, user_id, year=None, month=None, limit=None):
        """Get transactions for user"""
        async with db_lock:
            cursor = self.conn.cursor()
            query = "SELECT * FROM transactions WHERE user_id = ?"
            params = [user_id]

            if year and month:
                query += " AND strftime('%Y', date) = ? AND strftime('%m', date) = ?"
                params.extend([str(year), f"{month:02d}"])

            query += " ORDER BY timestamp DESC"

            if limit:
                query += f" LIMIT {limit}"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [dict(row) for row in rows]

    async def get_time_tracks(self, user_id, year=None, month=None, limit=None):
        """Get time tracks for user"""
        async with db_lock:
            cursor = self.conn.cursor()
            query = "SELECT * FROM time_tracks WHERE user_id = ?"
            params = [user_id]

            if year and month:
                query += " AND strftime('%Y', date) = ? AND strftime('%m', date) = ?"
                params.extend([str(year), f"{month:02d}"])

            query += " ORDER BY timestamp DESC"

            if limit:
                query += f" LIMIT {limit}"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [dict(row) for row in rows]

    async def get_all_transactions(self, user_id):
        """Get all transactions for user"""
        return await self.get_transactions(user_id)

    async def get_all_time_tracks(self, user_id):
        """Get all time tracks for user"""
        return await self.get_time_tracks(user_id)

    async def delete_transaction(self, transaction_id):
        """Delete specific transaction by ID"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            self.conn.commit()
            return cursor.rowcount > 0

    async def delete_time_track(self, track_id):
        """Delete specific time track by ID"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM time_tracks WHERE id = ?", (track_id,))
            self.conn.commit()
            return cursor.rowcount > 0

    async def clear_user_data(self, user_id):
        """Delete all transactions for user"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM time_tracks WHERE user_id = ?", (user_id,))
            self.conn.commit()
            return cursor.rowcount

    async def get_months_with_data(self, user_id):
        """Get list of months that have transactions"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT DISTINCT
                    strftime('%Y', date) as year,
                    strftime('%m', date) as month
                FROM transactions
                WHERE user_id = ?
                ORDER BY year DESC, month DESC
            ''', (user_id,))
            return cursor.fetchall()

    async def get_months_with_time_data(self, user_id):
        """Get list of months that have time tracks"""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT DISTINCT
                    strftime('%Y', date) as year,
                    strftime('%m', date) as month
                FROM time_tracks
                WHERE user_id = ?
                ORDER BY year DESC, month DESC
            ''', (user_id,))
            return cursor.fetchall()


# ========== SETTINGS MANAGEMENT ==========
def load_settings():
    """Load settings from file or create default"""
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            settings = json.load(f)
            for key in DEFAULT_SETTINGS:
                if key not in settings:
                    settings[key] = DEFAULT_SETTINGS[key]
            return settings
    except FileNotFoundError:
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS


def save_settings(settings):
    """Save settings to file"""
    with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# Load settings and create dynamic categories
SETTINGS = load_settings()
EMPLOYEES = SETTINGS['employees']
TAX_CONFIG = SETTINGS['tax_config']
CATEGORIES = SETTINGS['categories']
TIME_CATEGORIES = SETTINGS.get('time_categories', DEFAULT_SETTINGS['time_categories'])

# Add employee categories dynamically
def rebuild_employee_categories():
    """Rebuild employee categories from settings"""
    global CATEGORIES, EMPLOYEES

    # Remove old employee categories
    CATEGORIES['income'] = {k: v for k, v in CATEGORIES['income'].items() if not k.startswith('Від ')}
    CATEGORIES['expense'] = {k: v for k, v in CATEGORIES['expense'].items() if not k.startswith('ЗП ')}

    # Add new employee categories
    for emp in EMPLOYEES:
        CATEGORIES['income'][f'Від {emp}'] = {
            'emoji': '👤',
            'keywords': [emp.lower(), f'від {emp.lower()}']
        }
        CATEGORIES['expense'][f'ЗП {emp}'] = {
            'emoji': '💼',
            'keywords': [f'зп {emp.lower()}', f'зарплата {emp.lower()}']
        }

rebuild_employee_categories()


# Initialize database
db = Database()


# ========== EXCHANGE RATES ==========
async def update_exchange_rates():
    """Update exchange rates from NBU API"""
    global exchange_rates_cache

    try:
        async with aiohttp.ClientSession() as session:
            url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"

            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()

                    for item in data:
                        if item['cc'] == 'USD':
                            exchange_rates_cache['USD'] = float(item['rate'])
                        elif item['cc'] == 'EUR':
                            exchange_rates_cache['EUR'] = float(item['rate'])

                    exchange_rates_cache['last_update'] = datetime.now(KYIV_TZ)
                    logger.info(f"Exchange rates updated: USD={exchange_rates_cache['USD']}, EUR={exchange_rates_cache['EUR']}")
    except Exception as e:
        logger.error(f"Error updating exchange rates: {e}")


async def get_exchange_rate(currency):
    """Get exchange rate for currency"""
    global exchange_rates_cache

    if currency == 'UAH':
        return 1.0

    last_update = exchange_rates_cache.get('last_update')
    if not last_update or (datetime.now(KYIV_TZ) - last_update).seconds > 1800:
        await update_exchange_rates()

    return exchange_rates_cache.get(currency, 1.0)


def convert_to_uah(amount, currency, rate):
    """Convert amount to UAH"""
    if currency == 'UAH':
        return amount
    return amount * rate


# ========== UTILITY FUNCTIONS ==========
def parse_transaction(text):
    """Parse transaction from text"""
    text_lower = text.lower().strip()

    is_income = False
    if text.startswith('+'):
        is_income = True
    elif text.startswith('-'):
        is_income = False
    elif any(word in text_lower for word in ['зарплата', 'salary', 'дохід', 'отримав', 'прибуток']):
        is_income = True

    trans_type = 'income' if is_income else 'expense'

    amount_match = re.search(r'(\d+(?:[.,]\d{1,2})?)', text)
    if not amount_match:
        return None

    amount_str = amount_match.group(1).replace(',', '.')
    amount = float(amount_str)

    currency = 'UAH'
    if any(word in text_lower for word in ['usd', 'долар', 'доллар', '$']):
        currency = 'USD'
    elif any(word in text_lower for word in ['eur', 'євро', 'euro', '€']):
        currency = 'EUR'

    category = 'Інше'
    for cat_name, cat_data in CATEGORIES[trans_type].items():
        if any(kw in text_lower for kw in cat_data['keywords']):
            category = cat_name
            break

    date = datetime.now(KYIV_TZ)

    return {
        'amount': amount,
        'currency': currency,
        'type': trans_type,
        'category': category,
        'description': text,
        'date': date.strftime('%Y-%m-%d'),
        'timestamp': date.strftime('%Y-%m-%d %H:%M:%S')
    }


def parse_time_input(text):
    """Parse time from text like '90', '1.5год', '2h 30m'"""
    text = text.lower().strip()
    
    # Pattern: "90" or "90хв"
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(?:хв|м|min|minute|minutes)?$', text)
    if match:
        return int(float(match.group(1)))
    
    # Pattern: "1.5год" or "2h"
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(?:год|h|hour|hours)$', text)
    if match:
        return int(float(match.group(1)) * 60)
    
    # Pattern: "2год 30хв" or "2h 30m"
    match = re.match(r'^(\d+)\s*(?:год|h)\s+(\d+)\s*(?:хв|м|min)?$', text)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        return hours * 60 + minutes
    
    # Just a number
    try:
        return int(text)
    except:
        return None


# ========== KEYBOARD FUNCTIONS ==========
def get_main_keyboard():
    """Create main menu keyboard"""
    keyboard = [
        [KeyboardButton("💰 Баланс"), KeyboardButton("📊 Звіт")],
        [KeyboardButton("📂 Додати"), KeyboardButton("📜 Історія")],
        [KeyboardButton("⚙️ Налаштування"), KeyboardButton("ℹ️ Інфо")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_transaction_type_keyboard():
    """Create transaction type selection keyboard"""
    keyboard = [
        [InlineKeyboardButton("💰 Прибутки", callback_data="type:income")],
        [InlineKeyboardButton("💸 Витрати", callback_data="type:expense")],
        [InlineKeyboardButton("⏱️ Затрачений час", callback_data="time:select")],  # NEW!
        [InlineKeyboardButton("↩️ Відмінити останню", callback_data="undo:last")],
        [InlineKeyboardButton("◀️ Скасувати", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_time_category_keyboard():
    """Create time category selection keyboard"""
    keyboard = []
    
    # Group in rows of 2
    row = []
    for cat_name, cat_data in TIME_CATEGORIES.items():
        emoji = cat_data['emoji']
        button = InlineKeyboardButton(
            f"{emoji} {cat_name}",
            callback_data=f"timecat:{cat_name}"
        )
        row.append(button)
        
        if len(row) == 2:
            keyboard.append(row)
            row = []
    
    if row:
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="cancel")])
    
    return InlineKeyboardMarkup(keyboard)


def get_currency_keyboard(trans_type, category):
    """Create currency selection keyboard"""
    keyboard = [
        [InlineKeyboardButton("UAH ₴", callback_data=f"curr:{trans_type}:{category}:UAH")],
        [InlineKeyboardButton("USD $", callback_data=f"curr:{trans_type}:{category}:USD")],
        [InlineKeyboardButton("EUR €", callback_data=f"curr:{trans_type}:{category}:EUR")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"type:{trans_type}")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_category_keyboard(trans_type):
    """Create category selection keyboard"""
    keyboard = []
    categories = CATEGORIES[trans_type]

    if trans_type == 'expense':
        salary_cats = [k for k in categories.keys() if k.startswith('ЗП ')]
        other_cats = [k for k in categories.keys() if not k.startswith('ЗП ')]

        keyboard.append([InlineKeyboardButton("💼 ЗП працівникам", callback_data="submenu:salary")])

        row = []
        for cat_name in other_cats:
            cat_data = categories[cat_name]
            emoji = cat_data['emoji']
            button = InlineKeyboardButton(
                f"{emoji} {cat_name}",
                callback_data=f"cat:expense:{cat_name}"
            )
            row.append(button)

            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

    elif trans_type == 'income':
        employee_cats = [k for k in categories.keys() if k.startswith('Від ')]
        other_cats = [k for k in categories.keys() if not k.startswith('Від ')]

        row = []
        for cat_name in other_cats:
            cat_data = categories[cat_name]
            emoji = cat_data['emoji']
            button = InlineKeyboardButton(
                f"{emoji} {cat_name}",
                callback_data=f"cat:income:{cat_name}"
            )
            row.append(button)

            if len(row) == 2:
                keyboard.append(row)
                row = []

        if row:
            keyboard.append(row)

        keyboard.append([InlineKeyboardButton("👥 Від працівників", callback_data="submenu:employees")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="cancel")])

    return InlineKeyboardMarkup(keyboard)


def get_salary_submenu_keyboard():
    """Create salary payment submenu"""
    keyboard = []
    for emp in EMPLOYEES:
        keyboard.append([InlineKeyboardButton(
            f"💼 {emp}",
            callback_data=f"cat:expense:ЗП {emp}"
        )])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="type:expense")])
    return InlineKeyboardMarkup(keyboard)


def get_employee_income_submenu_keyboard():
    """Create employee income submenu"""
    keyboard = []
    for emp in EMPLOYEES:
        keyboard.append([InlineKeyboardButton(
            f"👤 {emp}",
            callback_data=f"cat:income:Від {emp}"
        )])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="type:income")])
    return InlineKeyboardMarkup(keyboard)


def get_settings_keyboard():
    """Create settings menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("👥 Працівники", callback_data="settings:employees")],
        [InlineKeyboardButton("📋 Категорії витрат", callback_data="settings:expense_cats")],
        [InlineKeyboardButton("💰 Категорії доходів", callback_data="settings:income_cats")],
        [InlineKeyboardButton("⏱️ Категорії часу", callback_data="settings:time_cats")],  # NEW!
        [InlineKeyboardButton("📊 Податки", callback_data="settings:tax")],
        [InlineKeyboardButton("◀️ Закрити", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_time_category_list_keyboard():
    """Create time category list keyboard for settings"""
    keyboard = []
    
    for cat_name, cat_data in TIME_CATEGORIES.items():
        emoji = cat_data['emoji']
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {cat_name}", callback_data=f"timecatview:{cat_name}"),
            InlineKeyboardButton("❌", callback_data=f"timecatdel:{cat_name}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Додати категорію", callback_data="timecatadd")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_employee_list_keyboard():
    """Create employee list keyboard"""
    keyboard = []
    for emp in EMPLOYEES:
        keyboard.append([
            InlineKeyboardButton(f"👤 {emp}", callback_data=f"emp_view:{emp}"),
            InlineKeyboardButton("❌", callback_data=f"emp_del:{emp}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Додати працівника", callback_data="emp_add")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_category_list_keyboard(cat_type):
    """Create category list keyboard"""
    keyboard = []
    categories = CATEGORIES[cat_type]

    # Filter out employee categories
    if cat_type == 'expense':
        cats_list = [k for k in categories.keys() if not k.startswith('ЗП ')]
    else:
        cats_list = [k for k in categories.keys() if not k.startswith('Від ')]

    for cat_name in cats_list:
        cat_data = categories[cat_name]
        emoji = cat_data['emoji']
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {cat_name}", callback_data=f"catview:{cat_type}:{cat_name}"),
            InlineKeyboardButton("❌", callback_data=f"catdel:{cat_type}:{cat_name}")
        ])

    keyboard.append([InlineKeyboardButton("➕ Додати категорію", callback_data=f"catadd:{cat_type}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_tax_settings_keyboard():
    """Create tax settings keyboard"""
    keyboard = [
        [InlineKeyboardButton("Єдиний податок", callback_data="tax_edit:single_tax")],
        [InlineKeyboardButton("ЄСВ (фіксований)", callback_data="tax_edit:esv")],
        [InlineKeyboardButton("◀️ Назад", callback_data="settings:main")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_report_menu_keyboard():
    """Create report menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("💰 Звіти за грошима", callback_data="report:money_menu")],  # MODIFIED
        [InlineKeyboardButton("⏱️ Звіти по часу", callback_data="report:time_menu")],  # NEW!
        [InlineKeyboardButton("◀️ Закрити", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_money_report_submenu_keyboard():
    """Create money reports submenu"""
    keyboard = [
        [InlineKeyboardButton("📊 Місячний поточний", callback_data="report:current")],
        [InlineKeyboardButton("📅 Місячний конкретний", callback_data="report:specific")],
        [InlineKeyboardButton("📈 Діаграма прибутків", callback_data="report:income_chart")],
        [InlineKeyboardButton("📉 Діаграма видатків", callback_data="report:expense_chart")],
        [InlineKeyboardButton("👥 По працівниках", callback_data="report:employees")],
        [InlineKeyboardButton("📋 Податковий звіт", callback_data="report:tax")],
        [InlineKeyboardButton("📚 Бухгалтерський звіт", callback_data="report:accounting")],
        [InlineKeyboardButton("🤖 Звіт для AI", callback_data="report:ai")],
        [InlineKeyboardButton("◀️ Назад", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_time_report_submenu_keyboard():
    """Create time reports submenu"""
    keyboard = [
        [InlineKeyboardButton("📊 Поточний місяць", callback_data="timereport:current")],
        [InlineKeyboardButton("📅 Конкретний місяць", callback_data="timereport:specific")],
        [InlineKeyboardButton("◀️ Назад", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def get_month_selection_keyboard(user_id):
    """Create month selection keyboard"""
    months_data = await db.get_months_with_data(user_id)

    if not months_data:
        return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="cancel")]])

    keyboard = []
    for row in months_data:
        year = int(row['year'])
        month = int(row['month'])
        month_name = MONTH_NAMES[month]
        keyboard.append([InlineKeyboardButton(
            f"{month_name} {year}",
            callback_data=f"month:{year}:{month}"
        )])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


async def get_time_month_selection_keyboard(user_id):
    """Create time month selection keyboard"""
    months_data = await db.get_months_with_time_data(user_id)

    if not months_data:
        return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="cancel")]])

    keyboard = []
    for row in months_data:
        year = int(row['year'])
        month = int(row['month'])
        month_name = MONTH_NAMES[month]
        keyboard.append([InlineKeyboardButton(
            f"{month_name} {year}",
            callback_data=f"timemonth:{year}:{month}"
        )])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)


def get_numpad_keyboard(current_amount, trans_type, category, currency):
    """Create numpad keyboard"""
    cat_encoded = category.replace(':', '_COLON_')

    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:1"),
            InlineKeyboardButton("2", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:2"),
            InlineKeyboardButton("3", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:3")
        ],
        [
            InlineKeyboardButton("4", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:4"),
            InlineKeyboardButton("5", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:5"),
            InlineKeyboardButton("6", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:6")
        ],
        [
            InlineKeyboardButton("7", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:7"),
            InlineKeyboardButton("8", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:8"),
            InlineKeyboardButton("9", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:9")
        ],
        [
            InlineKeyboardButton("⌫", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:back"),
            InlineKeyboardButton("0", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:0"),
            InlineKeyboardButton(".", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:dot")
        ],
        [
            InlineKeyboardButton("✅ Підтвердити", callback_data=f"num:{trans_type}:{cat_encoded}:{currency}:confirm"),
        ],
        [
            InlineKeyboardButton("◀️ Назад", callback_data=f"cat:{trans_type}:{cat_encoded}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def generate_text_chart(data_dict, total, title):
    """Generate text-based chart"""
    if not data_dict or total == 0:
        return "Немає даних"

    text = f"📊 {title}\n\n"

    sorted_items = sorted(data_dict.items(), key=lambda x: x[1], reverse=True)

    for category, amount in sorted_items:
        percentage = (amount / total) * 100
        emoji = '📦'
        for cat_type in ['income', 'expense']:
            if category in CATEGORIES[cat_type]:
                emoji = CATEGORIES[cat_type][category]['emoji']
                break

        bar_length = int((amount / total) * 30)
        bar = '█' * bar_length + '░' * (30 - bar_length)

        text += f"{emoji} {category}:\n"
        text += f"   {amount:.2f} грн ({percentage:.1f}%)\n"
        text += f"   {bar}\n\n"

    text += f"━━━━━━━━━━━━━━━━━━━━━━\n"
    text += f"💰 ЗАГАЛОМ: {total:.2f} грн"

    return text


# ========== COMMAND HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    await db.upsert_user(update.effective_user)
    await update.message.reply_text(
        "👋 Привіт! Я бот для обліку фінансів та часу @Olesia_money_bot\n\n"
        "📝 Ви можете:\n"
        "• Використовувати кнопки меню внизу\n"
        "• Писати текстом: `100 кава`, `зарплата 30000`\n"
        "• Валюти: `+50 USD консультація`, `100 EUR`\n"
        "• Відслідковувати час на активності ⏱️\n\n"
        "💡 Корисні команди:\n"
        "• **↩️ Відмінити** — в меню \"📂 Додати\"\n"
        "• `/settings` — працівники, категорії, податки\n"
        "• `/info` або кнопка **ℹ️ Інфо** — повна довідка\n\n"
        "Оберіть дію в меню внизу 👇",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )


async def show_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show info"""
    await update.message.reply_text(
        "ℹ️ **Фінансовий бот + Трекінг часу @Olesia_money_bot**\n\n"
        "💰 **ФІНАНСИ:**\n"
        "📱 Швидкий ввід текстом: `100 кава`, `зарплата 30000`\n"
        "💱 Валюти: `+50 USD`, `100 EUR`\n"
        "📊 Звіти, діаграми, податки, ROI працівників\n\n"
        "⏱️ **ТРЕКІНГ ЧАСУ:**\n"
        "1. Натисніть \"📂 Додати\" → \"⏱️ Затрачений час\"\n"
        "2. Оберіть категорію (Робота, Зал, Сон...)\n"
        "3. Введіть хвилини: `90` або `1.5год` або `2h 30m`\n"
        "4. Дані збережуться автоматично\n\n"
        "📊 **ЗВІТИ ПО ЧАСУ:**\n"
        "• Поточний місяць — швидка статистика\n"
        "• Конкретний місяць — вибір періоду\n"
        "• Аналіз продуктивності, статистика категорій\n\n"
        "⚙️ **НАЛАШТУВАННЯ:**\n"
        "• Додавайте свої категорії часу\n"
        "• Видаляйте непотрібні\n"
        "• Змінюйте емодзі\n\n"
        "💾 Всі дані зберігаються надійно в базі даних\n\n"
        "🔒 **ПРИВАТНІСТЬ:**\n"
        "• Дані кожного користувача ізольовані за Telegram ID\n"
        "• БД зберігається на Railway (хмарний хостинг, Europe/Kyiv TZ)\n"
        "• Power Olesia не має доступу до даних інших користувачів\n"
        "• `/clear` (через меню) видаляє всі ваші дані без можливості відновлення\n"
        "• Адміністратор отримує щодобовий зашифрований бекап БД\n"
        "• Telegram бачить лише ваші повідомлення боту, не БД",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show settings menu"""
    message = update.message if update.message else update.callback_query.message

    await message.reply_text(
        "⚙️ **НАЛАШТУВАННЯ**\n\n"
        "Оберіть розділ для редагування:",
        reply_markup=get_settings_keyboard(),
        parse_mode='Markdown'
    )


async def undo_last_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Undo last transaction"""
    user_id = str(update.effective_user.id)

    transactions = await db.get_transactions(user_id, limit=1)

    if not transactions:
        await update.message.reply_text(
            "📭 Немає транзакцій для відміни.",
            reply_markup=get_main_keyboard()
        )
        return

    last = transactions[0]

    emoji = "💸" if last['type'] == 'expense' else "💰"
    cat_emoji = CATEGORIES[last['type']].get(last['category'], {}).get('emoji', '📦')

    keyboard = [
        [InlineKeyboardButton("✅ Так, видалити", callback_data=f"undo_confirm:{last['id']}")],
        [InlineKeyboardButton("❌ Ні, залишити", callback_data="cancel")]
    ]

    amount_display = f"{last['amount']:.2f} {last['currency']}"
    if last['currency'] != 'UAH':
        amount_display += f" ({last['amount_uah']:.2f} грн)"

    await update.message.reply_text(
        f"⚠️ **ВІДМІНИТИ ОСТАННЮ ТРАНЗАКЦІЮ?**\n\n"
        f"{emoji} {amount_display}\n"
        f"{cat_emoji} Категорія: {last['category']}\n"
        f"📅 Дата: {last['date']}\n\n"
        f"Цю дію неможливо скасувати!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses from main keyboard"""
    text = update.message.text

    if text == "💰 Баланс":
        await show_balance(update, context)
    elif text == "📊 Звіт":
        await show_report_menu(update, context)
    elif text == "📂 Додати":
        await start_add_transaction(update, context)
    elif text == "📜 Історія":
        await show_history(update, context)
    elif text == "⚙️ Налаштування":
        await show_settings(update, context)
    elif text == "ℹ️ Інфо":
        await show_info(update, context)
    else:
        await handle_text_transaction(update, context)


async def start_add_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding transaction"""
    await update.message.reply_text(
        "📂 Додати транзакцію\n\nОберіть тип:",
        reply_markup=get_transaction_type_keyboard()
    )


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show balance"""
    user_id = str(update.effective_user.id)
    transactions = await db.get_all_transactions(user_id)

    if not transactions:
        await update.message.reply_text(
            "📭 У вас поки немає транзакцій.\n\nНапишіть щось на зразок: 100 кава",
            reply_markup=get_main_keyboard()
        )
        return

    total_income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense')
    balance = total_income - total_expense

    await update.message.reply_text(
        f"💼 **Ваш баланс:**\n\n"
        f"💰 Доходи: {total_income:.2f} грн\n"
        f"💸 Витрати: {total_expense:.2f} грн\n"
        f"━━━━━━━━━━━━\n"
        f"📊 Баланс: {balance:.2f} грн",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show last 15 transactions"""
    user_id = str(update.effective_user.id)
    transactions = await db.get_transactions(user_id, limit=15)

    if not transactions:
        await update.message.reply_text(
            "📭 У вас поки немає транзакцій.",
            reply_markup=get_main_keyboard()
        )
        return

    text = "📜 **Останні транзакції:**\n\n"
    for t in transactions:
        emoji = "💸" if t['type'] == 'expense' else "💰"
        cat_emoji = CATEGORIES[t['type']].get(t['category'], {}).get('emoji', '📦')

        amount_display = f"{t['amount']:.2f} {t['currency']}"
        if t['currency'] != 'UAH':
            amount_display += f" ({t['amount_uah']:.2f} грн)"

        text += f"{emoji} {amount_display} - {cat_emoji} {t['category']}\n"
        text += f"   📅 {t['date']}\n\n"

    await update.message.reply_text(
        text,
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )


async def show_report_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show report menu"""
    await update.message.reply_text(
        "📊 **Оберіть тип звіту:**",
        reply_markup=get_report_menu_keyboard(),
        parse_mode='Markdown'
    )


# ========== CALLBACK HANDLERS ==========
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    await query.answer()

    data_parts = query.data.split(':')
    action = data_parts[0]

    if action == "cancel":
        await query.edit_message_text("❌ Скасовано")
        return

    elif action == "time":
        # Handle time tracking
        if data_parts[1] == "select":
            await query.edit_message_text(
                "⏱️ **ЗАТРАЧЕНИЙ ЧАС**\n\nОберіть категорію:",
                reply_markup=get_time_category_keyboard(),
                parse_mode='Markdown'
            )
        return

    elif action == "timecat":
        # Time category selected
        category = ':'.join(data_parts[1:])
        context.user_data['waiting_for'] = f'time_minutes:{category}'
        
        cat_emoji = TIME_CATEGORIES.get(category, {}).get('emoji', '⏱️')
        
        await query.edit_message_text(
            f"{cat_emoji} **{category}**\n\n"
            f"⏱️ Скільки хвилин витратили?\n\n"
            f"Приклади:\n"
            f"• `90` — 90 хвилин\n"
            f"• `1.5год` — 1.5 години (90 хв)\n"
            f"• `2h 30m` — 2 год 30 хв (150 хв)",
            parse_mode='Markdown'
        )
        return

    elif action == "undo":
        # Handle undo from transaction menu
        if data_parts[1] == "last":
            user_id = str(query.from_user.id)
            transactions = await db.get_transactions(user_id, limit=1)

            if not transactions:
                await query.edit_message_text("📭 Немає транзакцій для відміни.")
                return

            last = transactions[0]

            emoji = "💸" if last['type'] == 'expense' else "💰"
            cat_emoji = CATEGORIES[last['type']].get(last['category'], {}).get('emoji', '📦')

            keyboard = [
                [InlineKeyboardButton("✅ Так, видалити", callback_data=f"undo_confirm:{last['id']}")],
                [InlineKeyboardButton("❌ Ні, залишити", callback_data="cancel")]
            ]

            amount_display = f"{last['amount']:.2f} {last['currency']}"
            if last['currency'] != 'UAH':
                amount_display += f" ({last['amount_uah']:.2f} грн)"

            await query.edit_message_text(
                f"⚠️ **ВІДМІНИТИ ОСТАННЮ ТРАНЗАКЦІЮ?**\n\n"
                f"{emoji} {amount_display}\n"
                f"{cat_emoji} Категорія: {last['category']}\n"
                f"📅 Дата: {last['date']}\n\n"
                f"Цю дію неможливо скасувати!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
        return

    elif action == "undo_confirm":
        transaction_id = int(data_parts[1])
        deleted = await db.delete_transaction(transaction_id)

        if deleted:
            await query.edit_message_text("✅ Транзакцію видалено!")
        else:
            await query.edit_message_text("❌ Помилка видалення")
        return

    elif action == "settings":
        await handle_settings_callback(update, context)
        return

    elif action == "timecatadd":
        context.user_data['waiting_for'] = 'time_category_name'
        await query.edit_message_text(
            "📝 **Додати категорію часу**\n\n"
            "Надішліть назву категорії (наприклад: Медитація)",
            parse_mode='Markdown'
        )
        return

    elif action == "timecatdel":
        cat_name = ':'.join(data_parts[1:])
        
        if cat_name in TIME_CATEGORIES and cat_name != 'Інше':
            del TIME_CATEGORIES[cat_name]
            SETTINGS['time_categories'] = TIME_CATEGORIES
            save_settings(SETTINGS)
            
            await query.edit_message_text(
                f"✅ Категорію \"{cat_name}\" видалено!",
                reply_markup=get_time_category_list_keyboard()
            )
        return

    elif action == "emp_add":
        context.user_data['waiting_for'] = 'employee_name'
        await query.edit_message_text(
            "👤 **Додати працівника**\n\n"
            "Надішліть ім'я працівника (наприклад: Олег)",
            parse_mode='Markdown'
        )
        return

    elif action == "emp_del":
        emp_name = ':'.join(data_parts[1:])
        if emp_name in EMPLOYEES:
            EMPLOYEES.remove(emp_name)
            SETTINGS['employees'] = EMPLOYEES
            save_settings(SETTINGS)
            rebuild_employee_categories()

            await query.edit_message_text(
                f"✅ Працівника \"{emp_name}\" видалено!",
                reply_markup=get_employee_list_keyboard()
            )
        return

    elif action == "catadd":
        cat_type = data_parts[1]
        context.user_data['waiting_for'] = f'category_name:{cat_type}'
        cat_type_name = "витрат" if cat_type == "expense" else "доходів"
        await query.edit_message_text(
            f"📝 **Додати категорію {cat_type_name}**\n\n"
            f"Надішліть назву категорії (наприклад: Оренда офісу)",
            parse_mode='Markdown'
        )
        return

    elif action == "catdel":
        cat_type = data_parts[1]
        cat_name = ':'.join(data_parts[2:])

        if cat_name in CATEGORIES[cat_type] and cat_name != 'Інше':
            del CATEGORIES[cat_type][cat_name]
            SETTINGS['categories'] = CATEGORIES
            save_settings(SETTINGS)

            cat_type_name = "витрат" if cat_type == "expense" else "доходів"
            await query.edit_message_text(
                f"✅ Категорію \"{cat_name}\" видалено!",
                reply_markup=get_category_list_keyboard(cat_type)
            )
        return

    elif action == "tax_edit":
        tax_type = data_parts[1]
        context.user_data['waiting_for'] = f'tax_value:{tax_type}'

        if tax_type == "single_tax":
            current = TAX_CONFIG['single_tax_rate'] * 100
            await query.edit_message_text(
                f"📝 **Зміна ставки єдиного податку**\n\n"
                f"Поточна ставка: {current:.0f}%\n\n"
                f"Надішліть нову ставку (наприклад: 3 для 3%)",
                parse_mode='Markdown'
            )
        elif tax_type == "esv":
            current = TAX_CONFIG['esv_fixed']
            await query.edit_message_text(
                f"📝 **Зміна фіксованого ЄСВ**\n\n"
                f"Поточна сума: {current:.0f} грн\n\n"
                f"Надішліть нову суму (наприклад: 1800)",
                parse_mode='Markdown'
            )
        return

    elif action == "type":
        trans_type = data_parts[1]
        type_name = "💰 Прибутки" if trans_type == "income" else "💸 Витрати"
        await query.edit_message_text(
            f"{type_name}\n\nОберіть категорію:",
            reply_markup=get_category_keyboard(trans_type)
        )

    elif action == "submenu":
        submenu_type = data_parts[1]
        if submenu_type == "salary":
            await query.edit_message_text(
                "💼 ЗП працівникам\n\nОберіть працівника:",
                reply_markup=get_salary_submenu_keyboard()
            )
        elif submenu_type == "employees":
            await query.edit_message_text(
                "👥 Від працівників\n\nОберіть працівника:",
                reply_markup=get_employee_income_submenu_keyboard()
            )

    elif action == "cat":
        trans_type = data_parts[1]
        category = ':'.join(data_parts[2:])
        category = category.replace('_COLON_', ':')

        context.user_data['trans_type'] = trans_type
        context.user_data['category'] = category

        cat_data = CATEGORIES[trans_type].get(category, {'emoji': '📦'})
        emoji = cat_data['emoji']

        await query.edit_message_text(
            f"{emoji} {category}\n\nОберіть валюту:",
            reply_markup=get_currency_keyboard(trans_type, category)
        )

    elif action == "curr":
        trans_type = data_parts[1]
        category = data_parts[2].replace('_COLON_', ':')
        currency = data_parts[3]

        context.user_data['trans_type'] = trans_type
        context.user_data['category'] = category
        context.user_data['currency'] = currency
        context.user_data['amount'] = ""

        cat_data = CATEGORIES[trans_type].get(category, {'emoji': '📦'})
        emoji = cat_data['emoji']

        currency_symbol = {'UAH': '₴', 'USD': '$', 'EUR': '€'}.get(currency, '')

        await query.edit_message_text(
            f"{emoji} {category}\n💱 Валюта: {currency} {currency_symbol}\n\n💰 Введіть суму: 0_",
            reply_markup=get_numpad_keyboard("", trans_type, category, currency)
        )

    elif action == "num":
        await handle_numpad(update, context)

    elif action == "report":
        await handle_report_callback(update, context)

    elif action == "month":
        year = int(data_parts[1])
        month = int(data_parts[2])
        await show_monthly_report(update, context, year, month)

    elif action == "timereport":
        await handle_time_report_callback(update, context)

    elif action == "timemonth":
        year = int(data_parts[1])
        month = int(data_parts[2])
        await show_time_monthly_report(update, context, year, month)


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle settings menu callbacks"""
    query = update.callback_query
    setting_type = query.data.split(':')[1]

    if setting_type == "main":
        await query.edit_message_text(
            "⚙️ **НАЛАШТУВАННЯ**\n\nОберіть розділ:",
            reply_markup=get_settings_keyboard(),
            parse_mode='Markdown'
        )

    elif setting_type == "employees":
        text = "👥 **ПРАЦІВНИКИ**\n\n"
        text += "Поточний список:\n"
        for i, emp in enumerate(EMPLOYEES, 1):
            text += f"{i}. {emp}\n"
        text += "\nНатисніть ❌ щоб видалити або ➕ щоб додати"

        await query.edit_message_text(
            text,
            reply_markup=get_employee_list_keyboard(),
            parse_mode='Markdown'
        )

    elif setting_type == "expense_cats":
        text = "📋 **КАТЕГОРІЇ ВИТРАТ**\n\n"
        text += "Натисніть на категорію для перегляду або ❌ для видалення\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_category_list_keyboard('expense'),
            parse_mode='Markdown'
        )

    elif setting_type == "income_cats":
        text = "💰 **КАТЕГОРІЇ ДОХОДІВ**\n\n"
        text += "Натисніть на категорію для перегляду або ❌ для видалення\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_category_list_keyboard('income'),
            parse_mode='Markdown'
        )

    elif setting_type == "time_cats":
        text = "⏱️ **КАТЕГОРІЇ ЧАСУ**\n\n"
        text += "Натисніть на категорію для перегляду або ❌ для видалення\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_time_category_list_keyboard(),
            parse_mode='Markdown'
        )

    elif setting_type == "tax":
        text = f"📊 **ПОДАТКОВІ НАЛАШТУВАННЯ**\n\n"
        text += f"Єдиний податок: {TAX_CONFIG['single_tax_rate']*100:.0f}%\n"
        text += f"ЄСВ (фіксований): {TAX_CONFIG['esv_fixed']:.0f} грн\n\n"
        text += f"💡 {TAX_CONFIG['note']}\n\n"
        text += "Натисніть кнопку для зміни"

        await query.edit_message_text(
            text,
            reply_markup=get_tax_settings_keyboard(),
            parse_mode='Markdown'
        )


async def handle_numpad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle numpad button presses"""
    query = update.callback_query

    parts = query.data.split(':')
    trans_type = parts[1]
    category = parts[2].replace('_COLON_', ':')
    currency = parts[3]
    action = parts[4]

    current_amount = context.user_data.get('amount', '')

    if action == "back":
        current_amount = current_amount[:-1] if current_amount else ""
    elif action == "dot":
        if '.' not in current_amount and current_amount:
            current_amount += '.'
    elif action == "confirm":
        if current_amount and current_amount != '.':
            await save_transaction(update, context, trans_type, category, currency, current_amount)
            return
        else:
            await query.answer("⚠️ Введіть суму!", show_alert=True)
            return
    else:
        current_amount += action

    context.user_data['amount'] = current_amount

    display_amount = current_amount if current_amount else "0"
    cat_data = CATEGORIES[trans_type].get(category, {'emoji': '📦'})
    emoji = cat_data['emoji']

    currency_symbol = {'UAH': '₴', 'USD': '$', 'EUR': '€'}.get(currency, '')

    await query.edit_message_text(
        f"{emoji} {category}\n💱 Валюта: {currency} {currency_symbol}\n\n💰 Введіть суму: {display_amount}_",
        reply_markup=get_numpad_keyboard(current_amount, trans_type, category, currency)
    )


async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, trans_type, category, currency, amount_str):
    """Save transaction to database"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    try:
        amount = float(amount_str)
    except ValueError:
        await query.answer("❌ Невірна сума!", show_alert=True)
        return

    rate = await get_exchange_rate(currency)
    amount_uah = convert_to_uah(amount, currency, rate)

    now = datetime.now(KYIV_TZ)

    await db.add_transaction(
        user_id=user_id,
        amount=amount,
        currency=currency,
        amount_uah=amount_uah,
        t_type=trans_type,
        category=category,
        description=f"{category} - {amount} {currency}",
        date=now.strftime('%Y-%m-%d'),
        timestamp=now.strftime('%Y-%m-%d %H:%M:%S')
    )

    emoji = "💸" if trans_type == "expense" else "💰"
    type_name = "Витрата" if trans_type == "expense" else "Дохід"
    cat_data = CATEGORIES[trans_type].get(category, {'emoji': '📦'})
    cat_emoji = cat_data['emoji']

    currency_symbol = {'UAH': '₴', 'USD': '$', 'EUR': '€'}.get(currency, '')

    response_text = (
        f"{emoji} **Записано!**\n\n"
        f"{cat_emoji} Категорія: {category}\n"
        f"💰 Сума: {amount:.2f} {currency} {currency_symbol}\n"
    )

    if currency != 'UAH':
        response_text += f"💱 В гривнях: {amount_uah:.2f} грн (курс: {rate:.2f})\n"

    response_text += (
        f"📅 Дата: {now.strftime('%Y-%m-%d')}\n"
        f"📋 Тип: {type_name}"
    )

    await query.edit_message_text(response_text, parse_mode='Markdown')


# ========== TEXT MESSAGE HANDLERS ==========
async def handle_text_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages"""
    # Check if waiting for specific input
    waiting_for = context.user_data.get('waiting_for')

    # Handle time input
    if waiting_for and waiting_for.startswith('time_minutes:'):
        category = waiting_for.replace('time_minutes:', '')
        text = update.message.text.strip()
        
        minutes = parse_time_input(text)
        
        if minutes is None or minutes <= 0:
            await update.message.reply_text(
                "❌ Не зрозумів. Спробуйте:\n"
                "• `90` — 90 хвилин\n"
                "• `1.5год` — 1.5 години\n"
                "• `2h 30m` — 2 год 30 хв",
                parse_mode='Markdown'
            )
            return
        
        user_id = str(update.effective_user.id)
        now = datetime.now(KYIV_TZ)
        
        await db.add_time_track(
            user_id=user_id,
            minutes=minutes,
            category=category,
            description=f"{category} - {minutes} хв",
            date=now.strftime('%Y-%m-%d'),
            timestamp=now.strftime('%Y-%m-%d %H:%M:%S')
        )
        
        cat_emoji = TIME_CATEGORIES.get(category, {}).get('emoji', '⏱️')
        hours = minutes / 60
        
        response_text = (
            f"⏱️ **Записано!**\n\n"
            f"{cat_emoji} Категорія: {category}\n"
            f"⏰ Час: {minutes} хв"
        )
        
        if hours >= 1:
            response_text += f" ({hours:.1f} год)"
        
        response_text += f"\n📅 Дата: {now.strftime('%Y-%m-%d')}"
        
        context.user_data['waiting_for'] = None
        
        await update.message.reply_text(
            response_text,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
        return

    # Handle time category name
    if waiting_for == 'time_category_name':
        cat_name = update.message.text.strip()
        
        if len(cat_name) < 2:
            await update.message.reply_text("❌ Назва занадто коротка. Спробуйте ще раз.")
            return
        
        if cat_name in TIME_CATEGORIES:
            await update.message.reply_text(f"⚠️ Категорія \"{cat_name}\" вже існує!")
            return
        
        context.user_data['new_time_category'] = {'name': cat_name}
        context.user_data['waiting_for'] = 'time_category_emoji'
        
        await update.message.reply_text(
            f"📝 Категорія: {cat_name}\n\n"
            f"Тепер надішліть емодзі для цієї категорії\n"
            f"(наприклад: 🧘 для медитації)"
        )
        return

    # Handle time category emoji
    if waiting_for == 'time_category_emoji':
        emoji = update.message.text.strip()
        
        if len(emoji) > 5:
            await update.message.reply_text("❌ Надішліть тільки один емодзі")
            return
        
        new_cat = context.user_data['new_time_category']
        cat_name = new_cat['name']
        
        TIME_CATEGORIES[cat_name] = {'emoji': emoji}
        SETTINGS['time_categories'] = TIME_CATEGORIES
        save_settings(SETTINGS)
        
        context.user_data['waiting_for'] = None
        context.user_data['new_time_category'] = None
        
        await update.message.reply_text(
            f"✅ Категорію \"{emoji} {cat_name}\" додано!",
            reply_markup=get_main_keyboard()
        )
        return

    if waiting_for == 'employee_name':
        emp_name = update.message.text.strip()

        if len(emp_name) < 2:
            await update.message.reply_text("❌ Ім'я занадто коротке. Спробуйте ще раз.")
            return

        if emp_name in EMPLOYEES:
            await update.message.reply_text(f"⚠️ Працівник \"{emp_name}\" вже існує!")
            return

        EMPLOYEES.append(emp_name)
        SETTINGS['employees'] = EMPLOYEES
        save_settings(SETTINGS)
        rebuild_employee_categories()

        context.user_data['waiting_for'] = None

        await update.message.reply_text(
            f"✅ Працівника \"{emp_name}\" додано!",
            reply_markup=get_main_keyboard()
        )
        return

    elif waiting_for and waiting_for.startswith('category_name:'):
        cat_type = waiting_for.split(':')[1]
        cat_name = update.message.text.strip()

        if len(cat_name) < 2:
            await update.message.reply_text("❌ Назва занадто коротка. Спробуйте ще раз.")
            return

        if cat_name in CATEGORIES[cat_type]:
            await update.message.reply_text(f"⚠️ Категорія \"{cat_name}\" вже існує!")
            return

        context.user_data['new_category'] = {'name': cat_name, 'type': cat_type}
        context.user_data['waiting_for'] = f'category_emoji:{cat_type}'

        await update.message.reply_text(
            f"📝 Категорія: {cat_name}\n\n"
            f"Тепер надішліть емодзі для цієї категорії\n"
            f"(наприклад: 🏢 для офісу)"
        )
        return

    elif waiting_for and waiting_for.startswith('category_emoji:'):
        emoji = update.message.text.strip()

        if len(emoji) > 5:
            await update.message.reply_text("❌ Надішліть тільки один емодзі")
            return

        context.user_data['new_category']['emoji'] = emoji
        context.user_data['waiting_for'] = f'category_keywords:{context.user_data["new_category"]["type"]}'

        await update.message.reply_text(
            f"📝 Категорія: {context.user_data['new_category']['name']} {emoji}\n\n"
            f"Тепер надішліть ключові слова для автоматичного розпізнавання\n"
            f"(через кому, наприклад: оренда, офіс, rent)"
        )
        return

    elif waiting_for and waiting_for.startswith('category_keywords:'):
        keywords_text = update.message.text.strip().lower()
        keywords = [k.strip() for k in re.split(r'[,\n]', keywords_text) if k.strip()]

        new_cat = context.user_data['new_category']
        cat_type = new_cat['type']
        cat_name = new_cat['name']
        emoji = new_cat['emoji']

        CATEGORIES[cat_type][cat_name] = {
            'emoji': emoji,
            'keywords': keywords
        }
        SETTINGS['categories'] = CATEGORIES
        save_settings(SETTINGS)

        context.user_data['waiting_for'] = None
        context.user_data['new_category'] = None

        await update.message.reply_text(
            f"✅ Категорію \"{emoji} {cat_name}\" додано!\n"
            f"Ключові слова: {', '.join(keywords)}",
            reply_markup=get_main_keyboard()
        )
        return

    elif waiting_for and waiting_for.startswith('tax_value:'):
        tax_type = waiting_for.split(':')[1]

        try:
            value = float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("❌ Невірне число. Спробуйте ще раз.")
            return

        if tax_type == "single_tax":
            if value < 1 or value > 20:
                await update.message.reply_text("❌ Ставка має бути від 1% до 20%")
                return

            TAX_CONFIG['single_tax_rate'] = value / 100
            SETTINGS['tax_config'] = TAX_CONFIG
            save_settings(SETTINGS)

            await update.message.reply_text(
                f"✅ Ставку єдиного податку змінено на {value:.0f}%",
                reply_markup=get_main_keyboard()
            )

        elif tax_type == "esv":
            if value < 500 or value > 10000:
                await update.message.reply_text("❌ Сума має бути від 500 до 10000 грн")
                return

            TAX_CONFIG['esv_fixed'] = value
            SETTINGS['tax_config'] = TAX_CONFIG
            save_settings(SETTINGS)

            await update.message.reply_text(
                f"✅ Фіксований ЄСВ змінено на {value:.0f} грн",
                reply_markup=get_main_keyboard()
            )

        context.user_data['waiting_for'] = None
        return

    # Try to parse as transaction
    text = update.message.text
    transaction = parse_transaction(text)

    if transaction:
        user_id = str(update.effective_user.id)

        rate = await get_exchange_rate(transaction['currency'])
        amount_uah = convert_to_uah(transaction['amount'], transaction['currency'], rate)

        await db.add_transaction(
            user_id=user_id,
            amount=transaction['amount'],
            currency=transaction['currency'],
            amount_uah=amount_uah,
            t_type=transaction['type'],
            category=transaction['category'],
            description=transaction['description'],
            date=transaction['date'],
            timestamp=transaction['timestamp']
        )

        emoji = "💸" if transaction['type'] == 'expense' else "💰"
        type_name = "Витрата" if transaction['type'] == 'expense' else "Дохід"
        cat_data = CATEGORIES[transaction['type']].get(transaction['category'], {'emoji': '📦'})
        cat_emoji = cat_data['emoji']

        currency_symbol = {'UAH': '₴', 'USD': '$', 'EUR': '€'}.get(transaction['currency'], '')

        response_text = (
            f"{emoji} **Записано!**\n\n"
            f"{cat_emoji} Категорія: {transaction['category']}\n"
            f"💰 Сума: {transaction['amount']:.2f} {transaction['currency']} {currency_symbol}\n"
        )

        if transaction['currency'] != 'UAH':
            response_text += f"💱 В гривнях: {amount_uah:.2f} грн (курс: {rate:.2f})\n"

        response_text += (
            f"📅 Дата: {transaction['date']}\n"
            f"📋 Тип: {type_name}"
        )

        await update.message.reply_text(
            response_text,
            reply_markup=get_main_keyboard(),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "❌ Не зрозумів. Спробуйте:\n"
            "• Кнопки меню\n"
            "• Текст: `100 кава`, `зарплата 30000`\n"
            "• З валютою: `+50 USD консультація`",
            reply_markup=get_main_keyboard()
        )


# ========== REPORT HANDLERS ==========
async def handle_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle report callbacks"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    report_type = query.data.split(':')[1]

    if report_type == "money_menu":
        await query.edit_message_text(
            "💰 **ЗВІТИ ЗА ГРОШИМА**\n\nОберіть тип звіту:",
            reply_markup=get_money_report_submenu_keyboard(),
            parse_mode='Markdown'
        )

    elif report_type == "time_menu":
        await query.edit_message_text(
            "⏱️ **ЗВІТИ ПО ЧАСУ**\n\nОберіть тип звіту:",
            reply_markup=get_time_report_submenu_keyboard(),
            parse_mode='Markdown'
        )

    elif report_type == "current":
        await show_monthly_report(update, context, datetime.now(KYIV_TZ).year, datetime.now(KYIV_TZ).month)

    elif report_type == "specific":
        keyboard = await get_month_selection_keyboard(user_id)
        await query.edit_message_text(
            "📅 **Оберіть місяць:**",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )

    elif report_type == "income_chart":
        await show_income_chart(update, context)

    elif report_type == "expense_chart":
        await show_expense_chart(update, context)

    elif report_type == "employees":
        await show_employee_report(update, context)

    elif report_type == "tax":
        await show_tax_report(update, context)

    elif report_type == "accounting":
        await show_accounting_report(update, context)

    elif report_type == "ai":
        await show_ai_report(update, context)


async def handle_time_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle time report callbacks"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    report_type = query.data.split(':')[1]

    if report_type == "current":
        await show_time_monthly_report(update, context, datetime.now(KYIV_TZ).year, datetime.now(KYIV_TZ).month)

    elif report_type == "specific":
        keyboard = await get_time_month_selection_keyboard(user_id)
        await query.edit_message_text(
            "📅 **Оберіть місяць:**",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )


async def show_time_monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE, year, month):
    """Show monthly time report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    time_tracks = await db.get_time_tracks(user_id, year, month)

    if not time_tracks:
        await query.edit_message_text(f"📭 Немає даних по часу за {MONTH_NAMES[month]} {year}.")
        return

    time_by_cat = defaultdict(int)
    total_minutes = 0

    for track in time_tracks:
        total_minutes += track['minutes']
        time_by_cat[track['category']] += track['minutes']

    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    total_hours = total_minutes / 60
    avg_per_day = total_hours / days_in_month

    text = f"⏱️ **ЗВІТ ПО ЧАСУ**\n{MONTH_NAMES[month]} {year}\n\n"
    text += f"━━━ ЗАГАЛЬНА СТАТИСТИКА ━━━\n"
    text += f"Відстежено: {total_minutes:,} хв ({total_hours:.1f} год)\n"
    text += f"Днів у місяці: {days_in_month}\n"
    text += f"Середньо/день: {avg_per_day:.1f} год\n\n"

    # Calculate productive/unproductive time
    productive_cats = ['Робота', 'Навчання', 'Підвищення кваліфікації', 'Уроки історії', 'Уроки англійської', 'Зал']
    unproductive_cats = ['Скрол стрічки', 'Розваги']
    rest_cats = ['Сон', 'Їжа', 'Відпустка']

    productive_time = sum(time_by_cat.get(cat, 0) for cat in productive_cats)
    unproductive_time = sum(time_by_cat.get(cat, 0) for cat in unproductive_cats)
    rest_time = sum(time_by_cat.get(cat, 0) for cat in rest_cats)

    text += f"━━━ ТОП КАТЕГОРІЙ ━━━\n"
    sorted_cats = sorted(time_by_cat.items(), key=lambda x: x[1], reverse=True)
    
    for i, (cat, minutes) in enumerate(sorted_cats[:10], 1):
        hours = minutes / 60
        percentage = (minutes / total_minutes * 100) if total_minutes > 0 else 0
        emoji = TIME_CATEGORIES.get(cat, {}).get('emoji', '⏱️')
        
        text += f"{i}. {emoji} {cat}: {minutes:,} хв ({hours:.1f} год) - {percentage:.1f}%\n"

    text += f"\n━━━ ПРОДУКТИВНІСТЬ ━━━\n"
    
    if productive_time > 0:
        prod_pct = (productive_time / total_minutes * 100)
        text += f"Корисний час: {productive_time:,} хв ({productive_time/60:.1f} год) - {prod_pct:.1f}% 🟢\n"
    
    if unproductive_time > 0:
        unprod_pct = (unproductive_time / total_minutes * 100)
        text += f"Непродуктивний: {unproductive_time:,} хв ({unproductive_time/60:.1f} год) - {unprod_pct:.1f}% 🟡\n"
    
    if rest_time > 0:
        rest_pct = (rest_time / total_minutes * 100)
        text += f"Відпочинок: {rest_time:,} хв ({rest_time/60:.1f} год) - {rest_pct:.1f}% 🔵\n"

    # Calculate untracked time
    total_minutes_in_month = days_in_month * 24 * 60
    untracked_minutes = total_minutes_in_month - total_minutes
    
    if untracked_minutes > 0:
        text += f"\n━━━ СЛІПІ ЗОНИ ━━━\n"
        text += f"Невідстежено: {untracked_minutes:,} хв ({untracked_minutes/60:.1f} год)\n"
        text += f"⚠️ Рекомендую відстежувати більше!"

    await query.edit_message_text(text, parse_mode='Markdown')


async def show_monthly_report(update: Update, context: ContextTypes.DEFAULT_TYPE, year, month):
    """Show monthly report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    transactions = await db.get_transactions(user_id, year, month)

    if not transactions:
        await query.edit_message_text(f"📭 Немає транзакцій за {MONTH_NAMES[month]} {year}.")
        return

    expenses_by_cat = defaultdict(float)
    income_by_cat = defaultdict(float)
    total_expense = 0
    total_income = 0

    for t in transactions:
        if t['type'] == 'expense':
            total_expense += t['amount_uah']
            expenses_by_cat[t['category']] += t['amount_uah']
        else:
            total_income += t['amount_uah']
            income_by_cat[t['category']] += t['amount_uah']

    text = f"📊 **Звіт за {MONTH_NAMES[month]} {year}:**\n\n"

    if income_by_cat:
        text += "💰 **Доходи:**\n"
        for cat in sorted(income_by_cat.items(), key=lambda x: x[1], reverse=True):
            emoji = CATEGORIES['income'].get(cat[0], {}).get('emoji', '📦')
            text += f"  {emoji} {cat[0]}: {cat[1]:.2f} грн\n"
        text += f"  💰 Разом: {total_income:.2f} грн\n\n"

    if expenses_by_cat:
        text += "💸 **Витрати:**\n"
        for cat in sorted(expenses_by_cat.items(), key=lambda x: x[1], reverse=True):
            emoji = CATEGORIES['expense'].get(cat[0], {}).get('emoji', '📦')
            text += f"  {emoji} {cat[0]}: {cat[1]:.2f} грн\n"
        text += f"  💸 Разом: {total_expense:.2f} грн\n\n"

    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"📊 **Баланс:** {total_income - total_expense:.2f} грн"

    await query.edit_message_text(text, parse_mode='Markdown')


async def show_income_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show income chart"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    income_by_cat = defaultdict(float)
    for t in transactions:
        if t['type'] == 'income':
            income_by_cat[t['category']] += t['amount_uah']

    if not income_by_cat:
        await query.edit_message_text("📭 Немає доходів.")
        return

    total_income = sum(income_by_cat.values())
    chart_text = generate_text_chart(income_by_cat, total_income, f"ДІАГРАМА ПРИБУТКІВ\n{MONTH_NAMES[current_date.month]} {current_date.year}")

    await query.edit_message_text(chart_text)


async def show_expense_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show expense chart"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    expense_by_cat = defaultdict(float)
    for t in transactions:
        if t['type'] == 'expense':
            expense_by_cat[t['category']] += t['amount_uah']

    if not expense_by_cat:
        await query.edit_message_text("📭 Немає витрат.")
        return

    total_expense = sum(expense_by_cat.values())
    chart_text = generate_text_chart(expense_by_cat, total_expense, f"ДІАГРАМА ВИДАТКІВ\n{MONTH_NAMES[current_date.month]} {current_date.year}")

    await query.edit_message_text(chart_text)


async def show_employee_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show employee report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    text = f"👥 **ЗВІТ ПО ПРАЦІВНИКАХ**\n{MONTH_NAMES[current_date.month]} {current_date.year}\n\n"

    for emp in EMPLOYEES:
        income_cat = f'Від {emp}'
        salary_cat = f'ЗП {emp}'

        income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income' and t['category'] == income_cat)
        expenses = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense' and t['category'] == salary_cat)

        if income > 0 or expenses > 0:
            profit = income - expenses
            profit_emoji = "✅" if profit > 0 else "⚠️" if profit == 0 else "❌"

            text += f"👤 **{emp}:**\n"
            text += f"  💰 Дохід: {income:.2f} грн\n"
            text += f"  💸 ЗП: {expenses:.2f} грн\n"
            text += f"  {profit_emoji} Прибуток: {profit:.2f} грн\n\n"

    await query.edit_message_text(text, parse_mode='Markdown')


async def show_tax_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tax report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    total_income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense')
    profit = total_income - total_expense

    single_tax_rate = TAX_CONFIG['single_tax_rate']
    esv_fixed = TAX_CONFIG['esv_fixed']

    single_tax = total_income * single_tax_rate
    total_tax = single_tax + esv_fixed

    import calendar
    last_day = calendar.monthrange(current_date.year, current_date.month)[1]

    text = f"📋 **ПОДАТКОВИЙ ЗВІТ**\n{MONTH_NAMES[current_date.month]} {current_date.year}\n\n"
    text += f"━━━ ЗАГАЛЬНІ ДАНІ ━━━\n"
    text += f"Період: 01.{current_date.month:02d}.{current_date.year} - {last_day}.{current_date.month:02d}.{current_date.year}\n\n"
    text += f"━━━ ДОХОДИ ━━━\n"
    text += f"Всього: {total_income:.2f} грн\n\n"
    text += f"━━━ ВИТРАТИ ━━━\n"
    text += f"Всього: {total_expense:.2f} грн\n\n"
    text += f"━━━ ПРИБУТОК ━━━\n"
    text += f"Чистий: {profit:.2f} грн\n\n"
    text += f"━━━ ПОДАТКИ ━━━\n"
    text += f"Єдиний ({single_tax_rate*100:.0f}%): {single_tax:.2f} грн\n"
    text += f"ЄСВ: {esv_fixed:.2f} грн\n"
    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"До сплати: {total_tax:.2f} грн\n\n"
    text += f"💰 Після податків: {profit - total_tax:.2f} грн"

    await query.edit_message_text(text, parse_mode='Markdown')


async def show_accounting_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show accounting report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    total_income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense')
    profit = total_income - total_expense

    prev_transactions = await db.get_all_transactions(user_id)

    prev_income = 0
    prev_expense = 0
    for t in prev_transactions:
        t_date = datetime.strptime(t['date'], '%Y-%m-%d')
        if t_date < datetime(current_date.year, current_date.month, 1):
            if t['type'] == 'income':
                prev_income += t['amount_uah']
            else:
                prev_expense += t['amount_uah']

    opening_balance = prev_income - prev_expense
    closing_balance = opening_balance + profit

    text = f"📚 **БУХГАЛТЕРСЬКИЙ ЗВІТ**\n{MONTH_NAMES[current_date.month]} {current_date.year}\n\n"
    text += f"━━━ БАЛАНС ━━━\n"
    text += f"Каса: {closing_balance:.2f} грн\n"
    text += f"Капітал: {opening_balance:.2f} грн\n"
    text += f"Прибуток: {profit:.2f} грн\n\n"
    text += f"━━━ ПРОВОДКИ ━━━\n"
    text += f"Дт 301 - Кт 701: {total_income:.2f} грн\n"
    text += f"Дт 901 - Кт 301: {total_expense:.2f} грн\n\n"
    text += f"━━━ РЕЗУЛЬТАТ ━━━\n"
    result_status = "прибуток ✅" if profit > 0 else "збиток ❌"
    text += f"{abs(profit):.2f} грн ({result_status})"

    await query.edit_message_text(text, parse_mode='Markdown')


async def show_ai_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate AI report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    income_by_cat = defaultdict(float)
    expense_by_cat = defaultdict(float)

    for t in transactions:
        if t['type'] == 'income':
            income_by_cat[t['category']] += t['amount_uah']
        else:
            expense_by_cat[t['category']] += t['amount_uah']

    total_income = sum(income_by_cat.values())
    total_expense = sum(expense_by_cat.values())
    balance = total_income - total_expense

    employees_roi = []
    for emp in EMPLOYEES:
        income = income_by_cat.get(f'Від {emp}', 0)
        salary = expense_by_cat.get(f'ЗП {emp}', 0)

        if income > 0 or salary > 0:
            roi = ((income - salary) / salary * 100) if salary > 0 else 0
            employees_roi.append({'name': emp, 'income': income, 'salary': salary, 'profit': income - salary, 'roi': roi})

    report = f"""🤖 АНАЛІЗ ФІНАНСІВ ДЛЯ AI

Ти фінансовий аналітик. Проаналізуй фінанси за {MONTH_NAMES[current_date.month]} {current_date.year}.

━━━ ЗАГАЛЬНА ІНФОРМАЦІЯ ━━━
Період: {MONTH_NAMES[current_date.month]} {current_date.year}
Дохід: {total_income:.2f} UAH
Витрати: {total_expense:.2f} UAH
Баланс: {balance:+.2f} UAH ({(balance/total_income*100 if total_income > 0 else 0):.1f}%)

━━━ ДОХОДИ ━━━
"""

    for i, (cat, amount) in enumerate(sorted(income_by_cat.items(), key=lambda x: x[1], reverse=True), 1):
        percentage = (amount / total_income * 100) if total_income > 0 else 0
        report += f"{i}. {cat}: {amount:.2f} UAH ({percentage:.1f}%)\n"

    report += "\n━━━ ВИТРАТИ ━━━\n"

    for i, (cat, amount) in enumerate(sorted(expense_by_cat.items(), key=lambda x: x[1], reverse=True), 1):
        percentage = (amount / total_expense * 100) if total_expense > 0 else 0
        report += f"{i}. {cat}: {amount:.2f} UAH ({percentage:.1f}%)\n"

    if employees_roi:
        report += "\n━━━ ROI ПРАЦІВНИКІВ ━━━\n"
        for emp_data in employees_roi:
            report += f"""{emp_data['name']}:
  Дохід: {emp_data['income']:.2f} UAH
  ЗП: {emp_data['salary']:.2f} UAH
  Прибуток: {emp_data['profit']:.2f} UAH
  ROI: {emp_data['roi']:.1f}%

"""

    report += """━━━ ЗАВДАННЯ ━━━
1. Оптимізація витрат
2. Ефективність працівників
3. Фінансові ризики
4. Можливості зростання
5. Рекомендації"""

    await query.edit_message_text(
        f"🤖 **ЗВІТ ДЛЯ AI**\n\n"
        f"📋 Скопіюйте текст нижче в ChatGPT/Gemini/Claude:\n\n"
        f"```\n{report}\n```",
        parse_mode='Markdown'
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show admin statistics — only for ADMIN_IDS"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна лише адміністратору.")
        return
    user_ids = await db.get_all_user_ids()
    cursor = db.conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transactions")
    tx_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM time_tracks")
    tt_count = cursor.fetchone()[0]
    await update.message.reply_text(
        f"📊 *Admin stats*\n\n"
        f"👥 Users: {len(user_ids)}\n"
        f"💸 Transactions: {tx_count}\n"
        f"⏱️ Time tracks: {tt_count}\n"
        f"📁 DB: `{DB_FILE}`\n"
        f"🔑 Admins: {len(ADMIN_IDS)}",
        parse_mode='Markdown'
    )


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Broadcast message to all users — admin only"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна лише адміністратору.")
        return
    full_text = update.message.text or ''
    parts = full_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "Використання: /broadcast <текст>\n"
            "Приклад: /broadcast 🛠 Бот оновлено, доступні нові функції."
        )
        return
    message_text = parts[1]
    user_ids = await db.get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("📭 Поки що немає користувачів у БД.")
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


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Send finance.db to first admin chat once per day"""
    if not ADMIN_IDS:
        return
    admin_id = sorted(ADMIN_IDS)[0]
    try:
        if not os.path.exists(DB_FILE):
            return
        ts = datetime.now(KYIV_TZ).strftime('%Y%m%d-%H%M')
        with open(DB_FILE, 'rb') as f:
            await context.bot.send_document(
                chat_id=int(admin_id),
                document=f,
                filename=f'finance-backup-{ts}.db',
                caption=f'🗄 Daily DB backup ({ts} Kyiv)'
            )
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'rb') as f:
                await context.bot.send_document(
                    chat_id=int(admin_id),
                    document=f,
                    filename=f'settings-backup-{ts}.json'
                )
        logger.info(f"daily backup sent to {admin_id}")
    except Exception as e:
        logger.warning(f"backup failed: {e}")


async def post_init_notify(application: Application):
    """Notify admins that bot has started"""
    if not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await application.bot.send_message(
                chat_id=int(admin_id),
                text=f"✅ Бот запущено та працює.\n"
                     f"📁 DB: `{DB_FILE}`\n"
                     f"👥 Admins: {len(ADMIN_IDS)}",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.warning(f"post_init notify failed for {admin_id}: {e}")


# ========== API SERVER (Mini App) ==========

_SKIP_AUTH_PATHS = {'/api/health', '/api/exchange-rates'}


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers to every response, handle OPTIONS preflight."""
    if request.method == 'OPTIONS':
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Telegram-Init-Data'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    return resp


@web.middleware
async def init_data_middleware(request: web.Request, handler):
    """Validate Telegram initData; attach user_id and tg_user to request."""
    if request.method == 'OPTIONS':
        return await handler(request)

    if request.path in _SKIP_AUTH_PATHS:
        return await handler(request)

    raw = (
        request.headers.get('X-Telegram-Init-Data')
        or request.rel_url.query.get('initData', '')
    )

    bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    parsed = validate_init_data(raw, bot_token) if raw else None

    if parsed is None:
        return web.json_response({'detail': 'Invalid initData'}, status=401)

    tg_user = parsed.get('user') or {}
    request['user_id'] = str(tg_user.get('id', ''))
    request['tg_user'] = tg_user
    # Auto-register the user so /admin_stats reflects real activity, even if
    # they only ever launched the Mini App and never sent /start.
    if tg_user.get('id'):
        try:
            await db.upsert_user(_UserObj(tg_user))
        except Exception as e:
            logger.warning(f'upsert_user via middleware failed: {e}')
    return await handler(request)


# ---- helpers ----

class _UserObj:
    """Minimal object that satisfies db.upsert_user(user) interface."""
    __slots__ = ('id', 'username', 'first_name', 'last_name', 'language_code')

    def __init__(self, d: dict):
        self.id = d.get('id', 0)
        self.username = d.get('username')
        self.first_name = d.get('first_name')
        self.last_name = d.get('last_name')
        self.language_code = d.get('language_code')


def _json_response(data, status=200):
    return web.json_response(data, status=status)


async def _ensure_fresh_rates():
    """Update exchange rates if stale (>30 min) or never fetched."""
    last = exchange_rates_cache.get('last_update')
    if last is None or (datetime.now(KYIV_TZ) - last).total_seconds() > 1800:
        await update_exchange_rates()


# ---- route handlers ----

async def api_health(request: web.Request):
    return _json_response({'ok': True, 'service': 'ruby-finance-api'})


async def api_me(request: web.Request):
    tg_user = request['tg_user']
    uid = request['user_id']
    return _json_response({
        'id': uid,
        'username': tg_user.get('username'),
        'first_name': tg_user.get('first_name'),
        'last_name': tg_user.get('last_name'),
        'is_admin': uid in ADMIN_IDS,
    })


async def api_exchange_rates(request: web.Request):
    await _ensure_fresh_rates()
    last = exchange_rates_cache.get('last_update')
    return _json_response({
        'USD': exchange_rates_cache.get('USD'),
        'EUR': exchange_rates_cache.get('EUR'),
        'updated_at': last.isoformat() if last else None,
    })


async def api_balance(request: web.Request):
    user_id = request['user_id']
    now = datetime.now(KYIV_TZ)
    try:
        year = int(request.rel_url.query.get('year', now.year))
        month = int(request.rel_url.query.get('month', now.month))
    except ValueError:
        return _json_response({'detail': 'Invalid year/month'}, status=400)

    rows = await db.get_transactions(user_id, year=year, month=month)
    income = sum(r['amount_uah'] for r in rows if r['type'] == 'income')
    expense = sum(r['amount_uah'] for r in rows if r['type'] == 'expense')
    return _json_response({
        'income': round(income, 2),
        'expense': round(expense, 2),
        'balance': round(income - expense, 2),
        'currency': 'UAH',
    })


async def api_get_transactions(request: web.Request):
    user_id = request['user_id']
    try:
        limit = min(int(request.rel_url.query.get('limit', 15)), 100)
    except ValueError:
        limit = 15

    rows = await db.get_transactions(user_id, limit=limit)
    result = [
        {
            'id': r['id'],
            'amount': r['amount'],
            'currency': r['currency'],
            'amount_uah': r['amount_uah'],
            'type': r['type'],
            'category': r['category'],
            'description': r['description'],
            'date': r['date'],
            'timestamp': r['timestamp'],
        }
        for r in rows
    ]
    return _json_response(result)


async def api_post_transaction(request: web.Request):
    user_id = request['user_id']
    tg_user = request['tg_user']

    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    t_type = body.get('type')
    if t_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)

    try:
        amount = float(body['amount'])
    except (KeyError, TypeError, ValueError):
        return _json_response({'detail': 'amount required and must be a number'}, status=400)

    currency = str(body.get('currency', 'UAH')).upper()
    if currency not in ('UAH', 'USD', 'EUR'):
        return _json_response({'detail': 'currency must be UAH, USD, or EUR'}, status=400)

    category = str(body.get('category', 'Інше'))
    description = body.get('description', '')

    rate = await get_exchange_rate(currency)
    amount_uah = round(convert_to_uah(amount, currency, rate), 2)

    now = datetime.now(KYIV_TZ)
    date_str = now.strftime('%Y-%m-%d')
    ts_str = now.strftime('%Y-%m-%d %H:%M:%S')

    row_id = await db.add_transaction(
        user_id, amount, currency, amount_uah,
        t_type, category, description, date_str, ts_str
    )

    await db.upsert_user(_UserObj(tg_user))

    logger.info(f"API POST /api/transactions user={user_id} id={row_id} {t_type} {amount} {currency}")
    return _json_response({
        'id': row_id,
        'amount': amount,
        'currency': currency,
        'amount_uah': amount_uah,
        'type': t_type,
        'category': category,
        'description': description,
        'date': date_str,
        'timestamp': ts_str,
    }, status=201)


async def api_delete_transaction(request: web.Request):
    user_id = request['user_id']
    try:
        tx_id = int(request.match_info['id'])
    except (KeyError, ValueError):
        return _json_response({'detail': 'Invalid id'}, status=400)

    # Verify ownership
    rows = await db.get_transactions(user_id)
    owned = any(r['id'] == tx_id for r in rows)
    if not owned:
        return _json_response({'detail': 'Not found'}, status=404)

    await db.delete_transaction(tx_id)
    logger.info(f"API DELETE /api/transactions/{tx_id} user={user_id}")
    return web.Response(status=204)


async def api_monthly_report(request: web.Request):
    user_id = request['user_id']
    now = datetime.now(KYIV_TZ)
    try:
        year = int(request.rel_url.query.get('year', now.year))
        month = int(request.rel_url.query.get('month', now.month))
    except ValueError:
        return _json_response({'detail': 'Invalid year/month'}, status=400)

    rows = await db.get_transactions(user_id, year=year, month=month)
    income_by_cat: dict[str, float] = {}
    expense_by_cat: dict[str, float] = {}
    total_income = 0.0
    total_expense = 0.0

    for r in rows:
        cat = r['category']
        amt = r['amount_uah']
        if r['type'] == 'income':
            income_by_cat[cat] = round(income_by_cat.get(cat, 0.0) + amt, 2)
            total_income += amt
        else:
            expense_by_cat[cat] = round(expense_by_cat.get(cat, 0.0) + amt, 2)
            total_expense += amt

    return _json_response({
        'income_by_category': income_by_cat,
        'expense_by_category': expense_by_cat,
        'total_income': round(total_income, 2),
        'total_expense': round(total_expense, 2),
        'transaction_count': len(rows),
    })


async def api_categories(request: web.Request):
    expense_names = [k for k in CATEGORIES.get('expense', {}).keys()]
    income_names = [k for k in CATEGORIES.get('income', {}).keys()]
    return _json_response({'expense': expense_names, 'income': income_names})


async def api_settings(request: web.Request):
    s = SETTINGS if SETTINGS else DEFAULT_SETTINGS
    return _json_response({
        'employees': s.get('employees', []),
        'tax_config': s.get('tax_config', {}),
    })


def build_api_app() -> web.Application:
    """Build and return the aiohttp API application."""
    app = web.Application(middlewares=[cors_middleware, init_data_middleware])
    app.router.add_route('GET', '/api/health', api_health)
    app.router.add_route('GET', '/api/me', api_me)
    app.router.add_route('GET', '/api/exchange-rates', api_exchange_rates)
    app.router.add_route('GET', '/api/balance', api_balance)
    app.router.add_route('GET', '/api/transactions', api_get_transactions)
    app.router.add_route('POST', '/api/transactions', api_post_transaction)
    app.router.add_route('DELETE', '/api/transactions/{id}', api_delete_transaction)
    app.router.add_route('GET', '/api/reports/monthly', api_monthly_report)
    app.router.add_route('GET', '/api/categories', api_categories)
    app.router.add_route('GET', '/api/settings', api_settings)
    # Catch-all OPTIONS for CORS preflight on any path
    app.router.add_route('OPTIONS', '/{path_info:.*}', lambda r: web.Response(status=204))
    return app


def main():
    """Start the bot"""
    import datetime as _dt
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8304522900:AAE9C8QXWjwo1BJ0Xwg2Vt5tXMcS3MSpOlk')

    if TOKEN == '8304522900:AAE9C8QXWjwo1BJ0Xwg2Vt5tXMcS3MSpOlk':
        logger.warning("⚠️ Using hardcoded token!")

    application = Application.builder().token(TOKEN).post_init(post_init_notify).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("info", show_info))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("admin_stats", admin_stats))
    application.add_handler(CommandHandler("stats", admin_stats))  # short alias
    application.add_handler(CommandHandler("admin", admin_stats))  # shorter alias
    application.add_handler(CommandHandler("broadcast", admin_broadcast))

    # Callbacks
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Text messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    # Daily DB backup at 03:00 Kyiv time
    if application.job_queue and ADMIN_IDS:
        application.job_queue.run_daily(
            daily_backup_job,
            time=_dt.time(hour=3, minute=0, tzinfo=KYIV_TZ),
            name='daily_backup'
        )
        logger.info("Daily backup scheduled at 03:00 Kyiv")

    logger.info("🤖 Бот @Olesia_money_bot запущено!")
    logger.info(f"📊 Database: {DB_FILE}")
    logger.info(f"⚙️ Settings: {SETTINGS_FILE}")
    logger.info(f"🔑 Admin IDs: {len(ADMIN_IDS)} configured")

    api_app = build_api_app()
    port = int(os.environ.get('PORT', 8080))

    async def run_all():
        await application.initialize()
        await application.start()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)

        runner = web.AppRunner(api_app, access_log=logger)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"API server started on port {port}")

        stop = asyncio.Event()
        try:
            await stop.wait()
        finally:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
            await runner.cleanup()

    asyncio.run(run_all())


if __name__ == '__main__':
    main()