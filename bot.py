import logging
import os
import sqlite3
import json
import hmac
import hashlib
import math
import secrets
import weakref
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import re
from collections import defaultdict
import asyncio
import aiohttp
from aiohttp import web
from dotenv import load_dotenv

from backup_service import (
    BackupError,
    S3BackupConfig,
    create_sqlite_snapshot,
    prune_remote_backups,
    upload_and_verify_snapshot,
)
from security_controls import TokenBucketLimiter
from finance_features import (
    SUPPORTED_FREQUENCIES,
    advance_recurrence,
    build_financial_insights,
    build_weekly_digest,
    detect_recurring_candidates,
    due_recurrence_dates,
    forecast_month_result,
    recurrence_occurrence_key,
)

load_dotenv()

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def parse_sentry_traces_sample_rate(raw_value, *, default=0.0) -> float:
    """Parse a Sentry sampling rate without allowing config to brick boot."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return float(default)
    return value


def sanitize_sentry_event(event, hint):
    """Remove credentials and financial request bodies before telemetry."""
    sanitized = dict(event or {})
    request = dict(sanitized.get('request') or {})
    headers = dict(request.get('headers') or {})
    sensitive_headers = {
        'authorization',
        'cookie',
        'set-cookie',
        'x-telegram-init-data',
    }
    request['headers'] = {
        key: value
        for key, value in headers.items()
        if str(key).lower() not in sensitive_headers
    }
    request.pop('data', None)
    request.pop('cookies', None)
    sanitized['request'] = request
    extra = dict(sanitized.get('extra') or {})
    for key in tuple(extra):
        if str(key).lower() in {'request_body', 'body', 'payload', 'init_data'}:
            extra.pop(key, None)
    sanitized['extra'] = extra
    breadcrumbs = dict(sanitized.get('breadcrumbs') or {})
    values = []
    for raw_crumb in breadcrumbs.get('values') or []:
        crumb = dict(raw_crumb or {})
        data = dict(crumb.get('data') or {})
        for key in tuple(data):
            if str(key).lower() in {
                'authorization', 'cookie', 'set-cookie',
                'x-telegram-init-data', 'request_body', 'body', 'payload',
                'init_data',
            }:
                data.pop(key, None)
        crumb['data'] = data
        values.append(crumb)
    breadcrumbs['values'] = values
    sanitized['breadcrumbs'] = breadcrumbs
    logentry = dict(sanitized.get('logentry') or {})
    logentry.pop('formatted', None)
    logentry.pop('params', None)
    sanitized['logentry'] = logentry
    return sanitized


try:
    import sentry_sdk

    if os.getenv('SENTRY_DSN'):
        try:
            sentry_sdk.init(
                dsn=os.environ['SENTRY_DSN'],
                traces_sample_rate=parse_sentry_traces_sample_rate(
                    os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0')
                ),
                send_default_pii=False,
                max_request_body_size='never',
                before_send=sanitize_sentry_event,
            )
        except Exception as exc:
            logger.warning('Sentry disabled because initialization failed: %s', type(exc).__name__)
except ImportError:
    sentry_sdk = None


# ========== TELEGRAM INIT DATA VALIDATION ==========
def _init_data_max_age_seconds() -> int:
    try:
        value = int(os.getenv('INIT_DATA_MAX_AGE_SECONDS', '21600'))
    except ValueError:
        value = 21600
    return value if value > 0 else 21600


def validate_init_data_result(
    raw_init_data: str,
    bot_token: str,
    max_age_seconds: int | None = None,
) -> tuple[dict | None, str | None]:
    """Validate signature first, then classify freshness for reliable UX."""
    if not bot_token:
        return None, 'INVALID_INIT_DATA'
    try:
        params = dict(parse_qsl(raw_init_data, keep_blank_values=True))
    except Exception:
        return None, 'INVALID_INIT_DATA'

    received_hash = params.pop('hash', None)
    if not received_hash:
        return None, 'INVALID_INIT_DATA'
    data_check_string = '\n'.join(
        f'{key}={value}' for key, value in sorted(params.items())
    )
    secret_key = hmac.new(
        b'WebAppData', bot_token.encode(), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        return None, 'INVALID_INIT_DATA'

    freshness = max_age_seconds
    if freshness is None:
        freshness = _init_data_max_age_seconds()
    if freshness <= 0:
        freshness = 21600
    try:
        auth_ts = int(params.get('auth_date', ''))
        age_seconds = datetime.now(timezone.utc).timestamp() - auth_ts
    except (TypeError, ValueError):
        return None, 'INVALID_INIT_DATA'
    if age_seconds > freshness:
        return None, 'INIT_DATA_EXPIRED'
    if age_seconds < -30:
        return None, 'INVALID_INIT_DATA'

    if 'user' in params:
        try:
            params['user'] = json.loads(params['user'])
        except (json.JSONDecodeError, TypeError):
            pass
    return params, None


def validate_init_data(raw_init_data: str, bot_token: str, max_age_seconds: int | None = None) -> dict | None:
    """Validate Telegram Mini App initData per official HMAC-SHA256 spec.

    Returns parsed dict (with user already JSON-decoded) on success, None on failure.
    """
    parsed, _error_code = validate_init_data_result(
        raw_init_data,
        bot_token,
        max_age_seconds=max_age_seconds,
    )
    return parsed


# Configuration
KYIV_TZ = ZoneInfo("Europe/Kyiv")
DATA_DIR = os.environ.get('DATA_DIR', '.')
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.environ.get('DB_FILE', os.path.join(DATA_DIR, 'finance.db'))
SETTINGS_FILE = os.environ.get('SETTINGS_FILE', os.path.join(DATA_DIR, 'settings.json'))
ADMIN_IDS = {x.strip() for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()}


def _bot_handle():
    raw = (
        os.getenv('TELEGRAM_BOT_HANDLE')
        or os.getenv('BOT_HANDLE')
        or '@ruby_finance_bot'
    ).strip()
    handle = raw if raw.startswith('@') else f'@{raw}'
    return handle if re.fullmatch(r'@[A-Za-z0-9_]{5,32}', handle) else '@ruby_finance_bot'


def _positive_env_int(name, default):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return value if value > 0 else default


def _env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


# Existing worker behavior remains enabled by default. A future redirect-only
# service must set this false, preventing duplicate finance/notification jobs.
ENABLE_SCHEDULED_JOBS = _env_flag('ENABLE_SCHEDULED_JOBS', default=True)


_preauth_limiter = TokenBucketLimiter.per_minute(
    _positive_env_int('RATE_LIMIT_PREAUTH_PER_MINUTE', 120),
    burst=_positive_env_int('RATE_LIMIT_PREAUTH_BURST', 60),
)
_read_limiter = TokenBucketLimiter.per_minute(
    _positive_env_int('RATE_LIMIT_READ_PER_MINUTE', 120),
    burst=_positive_env_int('RATE_LIMIT_READ_BURST', 60),
)
_write_limiter = TokenBucketLimiter.per_minute(
    _positive_env_int('RATE_LIMIT_WRITE_PER_MINUTE', 30),
    burst=_positive_env_int('RATE_LIMIT_WRITE_BURST', 20),
)
_admin_limiter = TokenBucketLimiter.per_minute(
    _positive_env_int('RATE_LIMIT_ADMIN_PER_MINUTE', 5),
    burst=_positive_env_int('RATE_LIMIT_ADMIN_BURST', 3),
)
_broadcast_limiter = TokenBucketLimiter(capacity=1, refill_rate=1 / 300)
_broadcast_confirmations: dict[str, dict] = {}


def _broadcast_text_digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _issue_broadcast_confirmation(
    admin_id,
    text: str,
    *,
    recipient_ids=None,
    ttl_seconds=600,
) -> tuple[str, str]:
    now = datetime.now(timezone.utc).timestamp()
    for stale_token, preview in tuple(_broadcast_confirmations.items()):
        if float(preview.get('expires_at', 0)) <= now:
            _broadcast_confirmations.pop(stale_token, None)
    token = secrets.token_urlsafe(24)
    expires_at = now + ttl_seconds
    _broadcast_confirmations[token] = {
        'admin_id': str(admin_id),
        'text_sha256': _broadcast_text_digest(text),
        'text': text,
        'recipient_ids': tuple(str(value) for value in (recipient_ids or ())),
        'expires_at': expires_at,
    }
    return token, datetime.fromtimestamp(expires_at, timezone.utc).isoformat()


def _broadcast_confirmation_matches(admin_id, text: str, token: str) -> bool:
    preview = _broadcast_confirmations.get(token)
    if not preview:
        return False
    now = datetime.now(timezone.utc).timestamp()
    matches = (
        preview.get('admin_id') == str(admin_id)
        and preview.get('text_sha256') == _broadcast_text_digest(text)
        and float(preview.get('expires_at', 0)) > now
    )
    return matches


def _consume_broadcast_confirmation(admin_id, text: str, token: str) -> bool:
    if not _broadcast_confirmation_matches(admin_id, text, token):
        return False
    _broadcast_confirmations.pop(token, None)
    return True


def _consume_chat_broadcast_confirmation(admin_id, token: str) -> str | None:
    preview = _broadcast_confirmations.get(token)
    if not preview:
        return None
    now = datetime.now(timezone.utc).timestamp()
    if (
        preview.get('admin_id') != str(admin_id)
        or float(preview.get('expires_at', 0)) <= now
    ):
        return None
    _broadcast_confirmations.pop(token, None)
    return str(preview.get('text') or '') or None

# Lock for database operations
db_lock = asyncio.Lock()


class _LoopLocalLockMap:
    """Per-key asyncio locks that never leak across separate event loops."""

    def __init__(self):
        self._loops = weakref.WeakKeyDictionary()

    def __getitem__(self, key):
        loop = asyncio.get_running_loop()
        locks = self._loops.setdefault(loop, {})
        return locks.setdefault(str(key), asyncio.Lock())


_recurring_user_locks = _LoopLocalLockMap()


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
    'USD': None,
    'EUR': None,
    'last_update': None
}


class ExchangeRateUnavailableError(RuntimeError):
    """Raised when no verified NBU rate or last-good value is available."""

backup_status = {
    'last_success': None,
    'last_error': None,
    'last_remote_key': None,
    'last_checksum': None,
}

# Official standard tax rules, versioned by report year. Fixed-tax values for
# groups 1/2 are the statutory maximums; users may override their local council
# rate in a year-specific profile. Special exemptions are intentionally not
# inferred automatically.
TAX_RULES_BY_YEAR = {
    2025: {
        'minimum_wage': 8000.0,
        'living_minimum': 3028.0,
        'fop1_fixed': 302.80,
        'fop2_fixed': 1600.0,
        'esv_fixed': 1760.0,
        'military_fixed': 800.0,
        'military_rate': 0.01,
    },
    2026: {
        'minimum_wage': 8647.0,
        'living_minimum': 3328.0,
        'fop1_fixed': 332.80,
        'fop2_fixed': 1729.40,
        'esv_fixed': 1902.34,
        'military_fixed': 864.70,
        'military_rate': 0.01,
    },
}


def current_tax_rules_year(*, now: datetime | None = None) -> int:
    """Return the explicitly supported Kyiv calendar year.

    We deliberately fail closed when the new year's official rules have not
    been encoded yet, and do not switch early merely because future rules are
    staged in the codebase.
    """
    moment = now or datetime.now(KYIV_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KYIV_TZ)
    calendar_year = moment.astimezone(KYIV_TZ).year
    if calendar_year not in TAX_RULES_BY_YEAR:
        raise ValueError(f'tax rules unavailable for {calendar_year}')
    return calendar_year


CURRENT_TAX_RULES_YEAR = current_tax_rules_year()
TAX_DISCLAIMER = (
    'Розрахунок інформаційний і не є податковою консультацією. '
    'ПДВ та спеціальні пільги автоматично не розраховуються.'
)


# Default settings
DEFAULT_SETTINGS = {
    # Universal neutral defaults — no personal/professional bias.
    # Each user is expected to add their own employees, time-categories,
    # and any niche expense/income categories via Settings → Категорії.
    'employees': [],
    'tax_config': {
        # Top-level values are compatibility fallbacks. Actual editable tax
        # profiles are stored by year so a future rules update cannot rewrite
        # a historical report.
        'group': 'fop3',
        'scheme': '5_percent',
        'profiles_by_year': {
            str(CURRENT_TAX_RULES_YEAR): {
                'group': 'fop3',
                'scheme': '5_percent',
            },
        },
        'note': TAX_DISCLAIMER,
    },
    'categories': {
        'expense': {
            'Продукти': {'emoji': '🛒', 'keywords': ['продукти', 'магазин', 'супермаркет', 'silpo', 'атб', 'groceries', 'їжа']},
            'Кафе': {'emoji': '☕', 'keywords': ['кава', 'кафе', 'coffee', 'ресторан', 'обід', 'lunch']},
            'Транспорт': {'emoji': '🚕', 'keywords': ['таксі', 'taxi', 'uber', 'bolt', 'метро', 'автобус', 'бензин']},
            'Розваги': {'emoji': '🎭', 'keywords': ['кіно', 'бар', 'клуб', 'пиво', 'cinema']},
            "Здоров'я": {'emoji': '💊', 'keywords': ['аптека', 'лікар', 'pharmacy', 'ліки']},
            'Одяг': {'emoji': '👗', 'keywords': ['одяг', 'clothes', 'взуття']},
            'Комунальні': {'emoji': '🏠', 'keywords': ['комунальні', 'комуналка', 'світло', 'газ', 'опалення']},
            'Податки': {'emoji': '📋', 'keywords': ['податки', 'tax', 'пдв', 'єдиний податок']},
            'Інше': {'emoji': '📦', 'keywords': []}
        },
        'income': {
            'Зарплата': {'emoji': '💰', 'keywords': ['зарплата', 'salary', 'зп']},
            'Фріланс': {'emoji': '💼', 'keywords': ['freelance', 'фріланс', 'проект']},
            'Інше': {'emoji': '📦', 'keywords': []}
        }
    },
    'time_categories': {
        'Сон': {'emoji': '😴'},
        'Робота': {'emoji': '💼'},
        'Зал': {'emoji': '🏋️'},
        'Їжа': {'emoji': '🍽️'},
        'Навчання': {'emoji': '🎓'},
        'Розваги': {'emoji': '🎉'},
        'Інше': {'emoji': '📦'}
    }
}

MONTH_NAMES = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
               'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень']


PAYMENT_SOURCES = ('cash', 'card', 'transfer', 'other')
PAYMENT_SOURCE_UNCLASSIFIED = 'unclassified'

# Feature-feedback announcement: per-feature 👍/👎 reaction keys + display labels.
FEEDBACK_FEATURE_LABELS = {
    'import': 'Імпорт виписок',
    'declaration': 'Декларація ФОП',
    'ai': 'AI-помічник',
    'ocr': 'Сканування чеків',
    'charts': 'Графіки потоку',
}


def _transaction_request_fingerprint(
    *, amount, currency, t_type, category, subcategory, payment_source,
    description,
):
    """Hash the immutable normalized create request, not mutable row fields."""
    normalized_amount = format(Decimal(str(amount)).normalize(), 'f')
    canonical = json.dumps(
        [
            normalized_amount,
            currency,
            t_type,
            category,
            subcategory,
            payment_source,
            description,
        ],
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


# Category labels are currently the relationship key. Keep all dependent-table
# writes in these registries so a future recurring-operations table can join
# the same atomic rename/delete transaction with one explicit hook.
CATEGORY_RENAME_DEPENDENCY_HOOKS = (
    ('transactions', '''
        UPDATE transactions SET category = ?
        WHERE user_id = ? AND type = ? AND category = ?
    '''),
    ('budgets', '''
        UPDATE budgets SET category = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND type = ? AND category = ?
    '''),
    ('recurring_operations', '''
        UPDATE recurring_operations
        SET category = ?, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND type = ? AND category = ?
    '''),
)
CATEGORY_DELETE_DEPENDENCY_HOOKS = (
    ('budgets', '''
        DELETE FROM budgets
        WHERE user_id = ? AND type = ? AND category = ?
    '''),
    ('recurring_operations', '''
        UPDATE recurring_operations
        SET active = 0, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = ? AND type = ? AND category = ?
    '''),
)


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
        # WAL mode lets the daily backup read a consistent snapshot while
        # writers (add_transaction etc.) continue without blocking.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            logger.warning(f'could not enable WAL: {e}')
        cursor = self.conn.cursor()
        # Migration tracker — lets us bake one-time data fixes into deploys
        # without ever asking users to run reset commands.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS _migrations (
                name TEXT PRIMARY KEY,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

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
                subcategory TEXT,
                description TEXT,
                date DATE NOT NULL,
                timestamp DATETIME NOT NULL,
                client_request_id TEXT,
                payment_source TEXT,
                request_fingerprint TEXT,
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
                client_request_id TEXT,
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

        # Per-user settings: each user owns their own employees, categories,
        # time categories, tax config. Stored as a JSON blob for schema-less
        # forward compatibility — every Mini App settings change writes here,
        # never the global SETTINGS file (which is now boot-time defaults only).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT PRIMARY KEY,
                settings_json TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                type TEXT NOT NULL,
                category TEXT NOT NULL,
                monthly_limit_uah REAL NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, type, category)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_budgets_user
            ON budgets(user_id)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recurring_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                type TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL,
                amount_uah REAL NOT NULL,
                category TEXT NOT NULL,
                subcategory TEXT,
                description TEXT,
                payment_source TEXT,
                frequency TEXT NOT NULL,
                interval INTEGER NOT NULL DEFAULT 1,
                start_date DATE NOT NULL,
                anchor_day INTEGER NOT NULL,
                next_due_date DATE NOT NULL,
                last_generated_date DATE,
                auto_create INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_recurring_user_due
            ON recurring_operations(user_id, active, next_due_date)
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id TEXT PRIMARY KEY,
                weekly_digest_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                period_key TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id INTEGER,
                error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, kind, period_key)
            )
        ''')

        # Broadcast audit trail: one row per broadcast batch, plus one receipt
        # row per recipient with the Telegram message_id (proof of delivery).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                sent INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                created_at DATETIME NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,          -- sent | failed | skipped
                message_id INTEGER,            -- Telegram message_id when sent
                reason TEXT,                   -- failure reason when failed
                created_at DATETIME NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_receipts_broadcast
            ON broadcast_receipts(broadcast_id)
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        self.conn.commit()

        # ── One-time migrations ────────────────────────────────────────
        # Wrapped in a global try/except: any migration failure must NOT
        # take the whole worker down (that would brick the API for every
        # user). On error we log and continue with the existing schema.
        try:
            self._run_migrations()
        except Exception as e:
            logger.exception(f"Migrations failed but continuing: {e}")

        logger.info("Database initialized successfully")

    def _run_migrations(self):
        """Apply any pending one-time data fixes. Each migration is
        individually try/excepted so one bad migration cannot block the
        others, nor crash the boot."""
        cursor = self.conn.cursor()

        def applied(name: str) -> bool:
            try:
                cursor.execute("SELECT 1 FROM _migrations WHERE name = ?", (name,))
                return cursor.fetchone() is not None
            except Exception:
                return False

        def mark(name: str):
            cursor.execute(
                "INSERT OR IGNORE INTO _migrations (name) VALUES (?)",
                (name,),
            )

        # 20260518_wipe_legacy_user_settings
        mig = '20260518_wipe_legacy_user_settings'
        if not applied(mig):
            try:
                # `user_settings` may not yet exist on a brand-new DB; guard.
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings'"
                )
                if cursor.fetchone():
                    cursor.execute("DELETE FROM user_settings")
                    wiped = cursor.rowcount
                else:
                    wiped = 0
                mark(mig)
                logger.info(f"Migration {mig}: wiped {wiped} legacy user_settings rows")
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        # 20260610_add_subcategory_column
        #   Hierarchical categories: a transaction may belong to an optional
        #   subcategory inside its category (e.g. Житло → Комунальні). Add a
        #   nullable column; existing rows keep subcategory = NULL.
        mig = '20260610_add_subcategory_column'
        if not applied(mig):
            try:
                cursor.execute("PRAGMA table_info(transactions)")
                cols = {r[1] for r in cursor.fetchall()}
                if 'subcategory' not in cols:
                    cursor.execute("ALTER TABLE transactions ADD COLUMN subcategory TEXT")
                    logger.info(f"Migration {mig}: added transactions.subcategory")
                mark(mig)
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        # 20260713_feature_feedback
        #   Announcement reaction buttons: per-feature 👍/👎 + free-text ideas.
        mig = '20260713_feature_feedback'
        if not applied(mig):
            try:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feature_reactions (
                        user_id TEXT NOT NULL,
                        feature TEXT NOT NULL,
                        reaction TEXT NOT NULL,
                        created_at TEXT,
                        PRIMARY KEY (user_id, feature)
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feature_comments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        comment TEXT NOT NULL,
                        created_at TEXT
                    )
                ''')
                mark(mig)
                logger.info(f"Migration {mig}: feedback tables ready")
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        # 20260713_import_batches
        #   CSV bank-statement import: each transaction carries an import_batch_id
        #   so a whole import can be previewed and rolled back atomically.
        mig = '20260713_import_batches'
        if not applied(mig):
            try:
                cursor.execute("PRAGMA table_info(transactions)")
                cols = {r[1] for r in cursor.fetchall()}
                if 'import_batch_id' not in cols:
                    cursor.execute(
                        "ALTER TABLE transactions ADD COLUMN import_batch_id INTEGER"
                    )
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS import_batches (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        source TEXT,
                        row_count INTEGER DEFAULT 0,
                        created_at TEXT
                    )
                ''')
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_tx_import_batch
                    ON transactions(user_id, import_batch_id)
                ''')
                mark(mig)
                logger.info(f"Migration {mig}: import_batches ready")
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        # 20260712_add_client_request_id
        #   Mini App writes can be retried after a timeout or a double tap. A
        #   caller-provided request id makes those retries safe, while scoping
        #   uniqueness by user keeps tenant data fully independent.
        mig = '20260712_add_client_request_id'
        if not applied(mig):
            try:
                cursor.execute("PRAGMA table_info(transactions)")
                cols = {r[1] for r in cursor.fetchall()}
                if 'client_request_id' not in cols:
                    cursor.execute(
                        "ALTER TABLE transactions ADD COLUMN client_request_id TEXT"
                    )
                    logger.info(
                        f"Migration {mig}: added transactions.client_request_id"
                    )
                cursor.execute('''
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_transactions_user_client_request
                    ON transactions(user_id, client_request_id)
                    WHERE client_request_id IS NOT NULL
                ''')
                mark(mig)
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        mig = '20260712_add_time_client_request_id'
        if not applied(mig):
            try:
                cursor.execute("PRAGMA table_info(time_tracks)")
                cols = {row[1] for row in cursor.fetchall()}
                if 'client_request_id' not in cols:
                    cursor.execute(
                        "ALTER TABLE time_tracks ADD COLUMN client_request_id TEXT"
                    )
                cursor.execute('''
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_time_tracks_user_client_request
                    ON time_tracks(user_id, client_request_id)
                    WHERE client_request_id IS NOT NULL
                ''')
                mark(mig)
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        # Legacy transactions intentionally remain NULL: source must be
        # supplied or corrected by the user, never guessed from category/text.
        mig = '20260712_add_payment_source'
        if not applied(mig):
            try:
                cursor.execute("PRAGMA table_info(transactions)")
                cols = {row[1] for row in cursor.fetchall()}
                if 'payment_source' not in cols:
                    cursor.execute(
                        "ALTER TABLE transactions ADD COLUMN payment_source TEXT"
                    )
                    logger.info(
                        f"Migration {mig}: added nullable transactions.payment_source"
                    )
                mark(mig)
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        mig = '20260712_add_request_fingerprint'
        if not applied(mig):
            try:
                cursor.execute("PRAGMA table_info(transactions)")
                cols = {row[1] for row in cursor.fetchall()}
                if 'request_fingerprint' not in cols:
                    cursor.execute(
                        "ALTER TABLE transactions ADD COLUMN request_fingerprint TEXT"
                    )
                cursor.execute('''
                    SELECT id, amount, currency, type, category, subcategory,
                           payment_source, description
                    FROM transactions
                    WHERE client_request_id IS NOT NULL
                      AND request_fingerprint IS NULL
                ''')
                for row in cursor.fetchall():
                    fingerprint = _transaction_request_fingerprint(
                        amount=row['amount'],
                        currency=row['currency'],
                        t_type=row['type'],
                        category=row['category'],
                        subcategory=row['subcategory'],
                        payment_source=row['payment_source'],
                        description=row['description'] or '',
                    )
                    cursor.execute('''
                        UPDATE transactions SET request_fingerprint = ?
                        WHERE id = ? AND request_fingerprint IS NULL
                    ''', (fingerprint, row['id']))
                mark(mig)
            except Exception as e:
                logger.warning(f"Migration {mig} failed (will retry next boot): {e}")

        self.conn.commit()

    async def get_user_settings(self, user_id):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT settings_json FROM user_settings WHERE user_id = ?",
                (str(user_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            try:
                return json.loads(row['settings_json'])
            except (json.JSONDecodeError, TypeError):
                return None

    async def save_user_settings(self, user_id, settings):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                '''
                INSERT INTO user_settings (user_id, settings_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    settings_json = excluded.settings_json,
                    updated_at = CURRENT_TIMESTAMP
                ''',
                (str(user_id), json.dumps(settings, ensure_ascii=False)),
            )
            self.conn.commit()

    async def delete_user_settings(self, user_id):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM user_settings WHERE user_id = ?", (str(user_id),))
            self.conn.commit()
            return cursor.rowcount > 0

    async def reset_user_settings(self, user_id):
        """Reset config atomically: delete budgets and pause all recurring.

        Historical transactions/time rows are intentionally retained.
        """
        owner = str(user_id)
        async with db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('BEGIN IMMEDIATE')
                cursor.execute("DELETE FROM budgets WHERE user_id = ?", (owner,))
                deleted_budgets = cursor.rowcount
                cursor.execute('''
                    UPDATE recurring_operations
                    SET active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND active = 1
                ''', (owner,))
                paused_recurring = cursor.rowcount
                cursor.execute(
                    "DELETE FROM user_settings WHERE user_id = ?", (owner,)
                )
                deleted_settings = cursor.rowcount
                self.conn.commit()
                return {
                    'budgets': deleted_budgets,
                    'recurring_operations_paused': paused_recurring,
                    'user_settings': deleted_settings,
                }
            except Exception:
                self.conn.rollback()
                raise

    async def delete_user_account(self, user_id):
        """Irreversibly delete every row owned by one Telegram user.

        The operation is intentionally one SQLite transaction: a partial
        privacy deletion is worse than a visible failure. Shared broadcast
        batches remain, while the user's per-recipient receipt is removed.
        """
        owner = str(user_id)
        tables = (
            'broadcast_receipts',
            'feature_reactions',
            'feature_comments',
            'import_batches',
            'transactions',
            'time_tracks',
            'budgets',
            'notification_deliveries',
            'notification_preferences',
            'recurring_operations',
            'subscriptions',
            'user_settings',
            'users',
        )
        async with _recurring_user_locks[owner]:
            async with db_lock:
                cursor = self.conn.cursor()
                deleted_rows = {}
                try:
                    cursor.execute('BEGIN IMMEDIATE')
                    for table in tables:
                        cursor.execute(
                            f'DELETE FROM {table} WHERE user_id = ?', (owner,)
                        )
                        deleted_rows[table] = cursor.rowcount
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
        return {'deleted_user_id': owner, 'deleted_rows': deleted_rows}

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

    async def get_all_users(self):
        """Full user roster with metadata — for the admin audit endpoint."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT user_id, username, first_name, last_name, first_seen, last_seen "
                "FROM users ORDER BY first_seen"
            )
            return [dict(r) for r in cursor.fetchall()]

    # ---- broadcast audit ----
    async def create_broadcast(self, text, created_at):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO broadcasts (text, created_at) VALUES (?, ?)",
                (text, created_at),
            )
            self.conn.commit()
            return cursor.lastrowid

    # ---- feature-feedback (announcement reactions + ideas) ----
    async def record_feature_reaction(self, user_id, feature, reaction, created_at):
        """One current 👍/👎 per (user, feature); re-tapping replaces the vote."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO feature_reactions (user_id, feature, reaction, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, feature) DO UPDATE SET "
                "reaction=excluded.reaction, created_at=excluded.created_at",
                (str(user_id), str(feature)[:40], str(reaction)[:10], created_at),
            )
            self.conn.commit()

    async def add_feature_comment(self, user_id, comment, created_at):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO feature_comments (user_id, comment, created_at) "
                "VALUES (?, ?, ?)",
                (str(user_id), str(comment)[:2000], created_at),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def get_feedback_summary(self):
        """Per-feature up/down tally + every reaction and free-text comment."""
        async with db_lock:
            cursor = self.conn.cursor()
            tally = {}
            try:
                cursor.execute(
                    "SELECT feature, reaction, COUNT(*) AS n FROM feature_reactions "
                    "GROUP BY feature, reaction"
                )
                for row in cursor.fetchall():
                    tally.setdefault(row['feature'], {'up': 0, 'down': 0})
                    tally[row['feature']][row['reaction']] = row['n']
                cursor.execute(
                    "SELECT user_id, feature, reaction, created_at FROM feature_reactions "
                    "ORDER BY created_at DESC"
                )
                reactions = [dict(r) for r in cursor.fetchall()]
                cursor.execute(
                    "SELECT id, user_id, comment, created_at FROM feature_comments "
                    "ORDER BY id DESC"
                )
                comments = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                logger.warning(f"get_feedback_summary failed: {e}")
                reactions, comments = [], []
            return {'tally': tally, 'reactions': reactions, 'comments': comments}

    async def save_broadcast_receipts(self, broadcast_id, receipts, created_at,
                                      sent, failed, skipped, total):
        """receipts: list of (user_id, status, message_id, reason)."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.executemany(
                "INSERT INTO broadcast_receipts "
                "(broadcast_id, user_id, status, message_id, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(broadcast_id, str(u), s, m, r, created_at) for (u, s, m, r) in receipts],
            )
            cursor.execute(
                "UPDATE broadcasts SET sent=?, failed=?, skipped=?, total=? WHERE id=?",
                (sent, failed, skipped, total, broadcast_id),
            )
            self.conn.commit()

    async def list_broadcasts(self, limit=20):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id, text, sent, failed, skipped, total, created_at "
                "FROM broadcasts ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            for row in rows:
                row['text_preview'] = (row.pop('text', '') or '')[:80]
            return rows

    async def get_broadcast_receipts(self, broadcast_id):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id, text, sent, failed, skipped, total, created_at "
                "FROM broadcasts WHERE id=?",
                (int(broadcast_id),),
            )
            b = cursor.fetchone()
            if not b:
                return None
            cursor.execute(
                "SELECT user_id, status, message_id, reason, created_at "
                "FROM broadcast_receipts WHERE broadcast_id=? ORDER BY id",
                (int(broadcast_id),),
            )
            receipts = [dict(r) for r in cursor.fetchall()]
            return {'broadcast': dict(b), 'receipts': receipts}

    async def log_admin_action(self, admin_id, action, *, target=None,
                               status='ok', metadata=None):
        """Persist a minimal forensic record without secrets or message text."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO admin_audit_log "
                "(admin_id, action, target, status, metadata_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(admin_id),
                    str(action)[:80],
                    None if target is None else str(target)[:120],
                    str(status)[:24],
                    json.dumps(metadata or {}, ensure_ascii=False)[:2000],
                ),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def list_admin_audit(self, limit=100):
        async with db_lock:
            rows = self.conn.execute(
                "SELECT id, admin_id, action, target, status, metadata_json, created_at "
                "FROM admin_audit_log ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(row) for row in rows]

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
                             category, description, date, timestamp, subcategory=None,
                             client_request_id=None, payment_source=None,
                             request_fingerprint=None, import_batch_id=None):
        """Add transaction to database"""
        async with db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO transactions
                    (user_id, amount, currency, amount_uah, type, category,
                     subcategory, description, date, timestamp, client_request_id,
                     payment_source, request_fingerprint, import_batch_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, amount, currency, amount_uah, t_type, category,
                    subcategory, description, date, timestamp, client_request_id,
                    payment_source, request_fingerprint, import_batch_id,
                ))
                self.conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                self.conn.rollback()
                raise

    # ---- CSV import batches (Block 4) ----
    async def create_import_batch(self, user_id, source, created_at):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO import_batches (user_id, source, created_at) VALUES (?, ?, ?)",
                (str(user_id), str(source)[:80], created_at),
            )
            self.conn.commit()
            return cursor.lastrowid

    async def finalize_import_batch(self, batch_id, row_count):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "UPDATE import_batches SET row_count=? WHERE id=?",
                (int(row_count), int(batch_id)),
            )
            self.conn.commit()

    async def list_import_batches(self, user_id, limit=50):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT id, source, row_count, created_at FROM import_batches "
                "WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (str(user_id), int(limit)),
            )
            return [dict(r) for r in cursor.fetchall()]

    async def rollback_import_batch(self, user_id, batch_id):
        """Owner-scoped: delete every transaction from one import batch + the batch."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute(
                "DELETE FROM transactions WHERE user_id=? AND import_batch_id=?",
                (str(user_id), int(batch_id)),
            )
            deleted = cursor.rowcount
            cursor.execute(
                "DELETE FROM import_batches WHERE user_id=? AND id=?",
                (str(user_id), int(batch_id)),
            )
            self.conn.commit()
            return deleted

    async def get_transaction_by_client_request_id(
        self, user_id, client_request_id
    ):
        """Return one user's transaction for an idempotency key, if present."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT * FROM transactions
                WHERE user_id = ? AND client_request_id = ?
                LIMIT 1
            ''', (str(user_id), client_request_id))
            row = cursor.fetchone()
            return dict(row) if row else None

    async def get_quick_templates(self, user_id, limit=5):
        """Aggregate reusable templates over all of one user's history."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT
                    amount,
                    currency,
                    type,
                    category,
                    NULLIF(subcategory, '') AS subcategory,
                    COALESCE(description, '') AS description,
                    payment_source,
                    COUNT(*) AS usage_count,
                    MAX(timestamp) AS last_used_at
                FROM transactions
                WHERE user_id = ?
                GROUP BY
                    amount,
                    currency,
                    type,
                    category,
                    NULLIF(subcategory, ''),
                    COALESCE(description, ''),
                    payment_source
                ORDER BY usage_count DESC, last_used_at DESC
                LIMIT ?
            ''', (str(user_id), int(limit)))
            templates = [dict(row) for row in cursor.fetchall()]

            cursor.execute('''
                SELECT * FROM transactions
                WHERE user_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT 1
            ''', (str(user_id),))
            last_row = cursor.fetchone()
            return templates, (dict(last_row) if last_row else None)

    async def add_time_track(
        self,
        user_id,
        minutes,
        category,
        description,
        date,
        timestamp,
        client_request_id=None,
    ):
        """Add time track to database"""
        async with db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO time_tracks
                    (user_id, minutes, category, description, date, timestamp,
                     client_request_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, minutes, category, description, date, timestamp,
                    client_request_id,
                ))
                self.conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                self.conn.rollback()
                raise

    async def get_time_track_by_client_request_id(
        self, user_id, client_request_id
    ):
        async with db_lock:
            row = self.conn.execute('''
                SELECT * FROM time_tracks
                WHERE user_id = ? AND client_request_id = ?
                LIMIT 1
            ''', (str(user_id), client_request_id)).fetchone()
            return dict(row) if row else None

    async def get_transactions(self, user_id, year=None, month=None, limit=None,
                                t_type=None, from_date=None, to_date=None):
        """Get transactions for user.

        Filters (all optional):
          • year+month  — restrict to a specific calendar month
          • t_type      — 'income' or 'expense'
          • from_date   — ISO 'YYYY-MM-DD' (inclusive lower bound)
          • to_date     — ISO 'YYYY-MM-DD' (inclusive upper bound)
          • limit       — at most N rows (use _parse_limit at the API layer)

        from_date/to_date take precedence over year/month if both are given.
        """
        async with db_lock:
            cursor = self.conn.cursor()
            query = "SELECT * FROM transactions WHERE user_id = ?"
            params = [user_id]

            if from_date or to_date:
                if from_date:
                    query += " AND date >= ?"
                    params.append(str(from_date))
                if to_date:
                    query += " AND date <= ?"
                    params.append(str(to_date))
            elif year and month:
                query += " AND strftime('%Y', date) = ? AND strftime('%m', date) = ?"
                params.extend([str(year), f"{month:02d}"])

            if t_type in ('income', 'expense'):
                query += " AND type = ?"
                params.append(t_type)

            query += " ORDER BY timestamp DESC"

            if limit:
                query += " LIMIT ?"
                params.append(int(limit))

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
                query += " LIMIT ?"
                params.append(int(limit))

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [dict(row) for row in rows]

    async def get_all_transactions(self, user_id):
        """Get all transactions for user"""
        return await self.get_transactions(user_id)

    async def get_all_time_tracks(self, user_id):
        """Get all time tracks for user"""
        return await self.get_time_tracks(user_id)

    async def update_transaction_payment_source(
        self, transaction_id, user_id, payment_source
    ):
        """Correct a source without allowing cross-tenant row discovery."""
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                UPDATE transactions
                SET payment_source = ?
                WHERE id = ? AND user_id = ?
            ''', (payment_source, int(transaction_id), str(user_id)))
            if cursor.rowcount == 0:
                self.conn.commit()
                return None
            cursor.execute(
                "SELECT * FROM transactions WHERE id = ? AND user_id = ?",
                (int(transaction_id), str(user_id)),
            )
            row = cursor.fetchone()
            self.conn.commit()
            return dict(row) if row else None

    async def upsert_budget(
        self, user_id, budget_type, category, monthly_limit_uah
    ):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO budgets
                    (user_id, type, category, monthly_limit_uah)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, type, category) DO UPDATE SET
                    monthly_limit_uah = excluded.monthly_limit_uah,
                    updated_at = CURRENT_TIMESTAMP
            ''', (
                str(user_id), budget_type, category, monthly_limit_uah,
            ))
            cursor.execute('''
                SELECT type, category, monthly_limit_uah
                FROM budgets
                WHERE user_id = ? AND type = ? AND category = ?
            ''', (str(user_id), budget_type, category))
            row = cursor.fetchone()
            self.conn.commit()
            return dict(row)

    async def get_budget_progress(self, user_id, from_date, to_date):
        """Return budgets with tenant-scoped spend over [from_date, to_date)."""
        async with db_lock:
            rows = self.conn.execute('''
                SELECT
                    b.type,
                    b.category,
                    b.monthly_limit_uah,
                    COALESCE(SUM(t.amount_uah), 0) AS spent_uah
                FROM budgets AS b
                LEFT JOIN transactions AS t
                    ON t.user_id = b.user_id
                   AND t.type = b.type
                   AND t.category = b.category
                   AND t.date >= ?
                   AND t.date < ?
                WHERE b.user_id = ?
                GROUP BY b.id, b.type, b.category, b.monthly_limit_uah
                ORDER BY b.type, b.category
            ''', (from_date, to_date, str(user_id))).fetchall()
            return [dict(row) for row in rows]

    async def delete_budget(self, user_id, budget_type, category):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                DELETE FROM budgets
                WHERE user_id = ? AND type = ? AND category = ?
            ''', (str(user_id), budget_type, category))
            self.conn.commit()
            return cursor.rowcount > 0

    async def get_budgets(self, user_id):
        async with db_lock:
            rows = self.conn.execute('''
                SELECT type, category, monthly_limit_uah
                FROM budgets
                WHERE user_id = ?
                ORDER BY type, category
            ''', (str(user_id),)).fetchall()
            return [dict(row) for row in rows]

    async def create_recurring_operation(self, user_id, values):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO recurring_operations (
                    user_id, type, amount, currency, amount_uah, category,
                    subcategory, description, payment_source, frequency,
                    interval, start_date, anchor_day, next_due_date,
                    last_generated_date, auto_create, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(user_id), values['type'], values['amount'], values['currency'],
                values['amount_uah'], values['category'], values['subcategory'],
                values['description'], values['payment_source'], values['frequency'],
                values['interval'], values['start_date'], values['anchor_day'],
                values['next_due_date'], values.get('last_generated_date'),
                int(values['auto_create']), int(values['active']),
            ))
            recurring_id = cursor.lastrowid
            row = cursor.execute(
                "SELECT * FROM recurring_operations WHERE id = ?",
                (recurring_id,),
            ).fetchone()
            self.conn.commit()
            return dict(row)

    async def get_recurring_operation(self, user_id, recurring_id):
        async with db_lock:
            row = self.conn.execute('''
                SELECT * FROM recurring_operations
                WHERE id = ? AND user_id = ?
            ''', (int(recurring_id), str(user_id))).fetchone()
            return dict(row) if row else None

    async def list_recurring_operations(self, user_id):
        async with db_lock:
            rows = self.conn.execute('''
                SELECT * FROM recurring_operations
                WHERE user_id = ?
                ORDER BY id
            ''', (str(user_id),)).fetchall()
            return [dict(row) for row in rows]

    async def update_recurring_operation(self, user_id, recurring_id, values):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                UPDATE recurring_operations SET
                    type = ?, amount = ?, currency = ?, amount_uah = ?,
                    category = ?, subcategory = ?, description = ?,
                    payment_source = ?, frequency = ?, interval = ?,
                    start_date = ?, anchor_day = ?, next_due_date = ?,
                    last_generated_date = ?, auto_create = ?, active = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
            ''', (
                values['type'], values['amount'], values['currency'],
                values['amount_uah'], values['category'], values['subcategory'],
                values['description'], values['payment_source'], values['frequency'],
                values['interval'], values['start_date'], values['anchor_day'],
                values['next_due_date'], values.get('last_generated_date'),
                int(values['auto_create']), int(values['active']),
                int(recurring_id), str(user_id),
            ))
            if cursor.rowcount == 0:
                self.conn.commit()
                return None
            row = cursor.execute('''
                SELECT * FROM recurring_operations
                WHERE id = ? AND user_id = ?
            ''', (int(recurring_id), str(user_id))).fetchone()
            self.conn.commit()
            return dict(row)

    async def delete_recurring_operation(self, user_id, recurring_id):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                DELETE FROM recurring_operations
                WHERE id = ? AND user_id = ?
            ''', (int(recurring_id), str(user_id)))
            self.conn.commit()
            return cursor.rowcount > 0

    async def list_due_recurring_operations(self, through_date):
        async with db_lock:
            rows = self.conn.execute('''
                SELECT * FROM recurring_operations
                WHERE active = 1 AND auto_create = 1 AND next_due_date <= ?
                ORDER BY id
            ''', (str(through_date),)).fetchall()
            return [dict(row) for row in rows]

    async def mark_recurring_generated(
        self, user_id, recurring_id, last_generated_date, next_due_date
    ):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                UPDATE recurring_operations
                SET last_generated_date = ?, next_due_date = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
            ''', (
                str(last_generated_date), str(next_due_date), int(recurring_id),
                str(user_id),
            ))
            self.conn.commit()
            return cursor.rowcount > 0

    async def materialize_recurring_occurrences(
        self,
        user_id,
        recurring_id,
        *,
        expected_next_due,
        expected_currency,
        due_dates,
        rate,
        next_due_date,
    ):
        """Atomically re-check, create due rows, and advance the template."""
        owner = str(user_id)
        async with db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('BEGIN IMMEDIATE')
                row = cursor.execute('''
                    SELECT * FROM recurring_operations
                    WHERE id = ? AND user_id = ?
                      AND active = 1 AND auto_create = 1
                ''', (int(recurring_id), owner)).fetchone()
                if (
                    row is None
                    or row['next_due_date'] != expected_next_due
                    or row['currency'] != expected_currency
                ):
                    self.conn.commit()
                    return {'created': 0, 'processed': False}

                amount_uah = round(convert_to_uah(
                    float(row['amount']), row['currency'], rate
                ), 2)
                if amount_uah < 0.01:
                    raise ValueError('UAH equivalent must be at least 0.01')
                created = 0
                for due_date in due_dates:
                    request_id = recurrence_occurrence_key(row['id'], due_date)
                    cursor.execute('''
                        INSERT OR IGNORE INTO transactions (
                            user_id, amount, currency, amount_uah, type,
                            category, subcategory, description, date, timestamp,
                            client_request_id, payment_source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        owner, row['amount'], row['currency'], amount_uah,
                        row['type'], row['category'], row['subcategory'],
                        row['description'], due_date.isoformat(),
                        f'{due_date.isoformat()} 12:00:00', request_id,
                        row['payment_source'],
                    ))
                    created += max(cursor.rowcount, 0)
                cursor.execute('''
                    UPDATE recurring_operations
                    SET last_generated_date = ?, next_due_date = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ? AND active = 1
                ''', (
                    due_dates[-1].isoformat(), next_due_date.isoformat(),
                    int(recurring_id), owner,
                ))
                processed = cursor.rowcount > 0
                self.conn.commit()
                return {'created': created, 'processed': processed}
            except Exception:
                self.conn.rollback()
                raise

    async def get_notification_preferences(self, user_id):
        async with db_lock:
            row = self.conn.execute('''
                SELECT weekly_digest_enabled
                FROM notification_preferences
                WHERE user_id = ?
            ''', (str(user_id),)).fetchone()
            return {
                'weekly_digest_enabled': bool(row['weekly_digest_enabled']) if row else False
            }

    async def set_notification_preferences(self, user_id, enabled):
        async with db_lock:
            self.conn.execute('''
                INSERT INTO notification_preferences
                    (user_id, weekly_digest_enabled, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    weekly_digest_enabled = excluded.weekly_digest_enabled,
                    updated_at = CURRENT_TIMESTAMP
            ''', (str(user_id), int(enabled)))
            self.conn.commit()
            return {'weekly_digest_enabled': bool(enabled)}

    async def list_weekly_digest_users(self):
        async with db_lock:
            rows = self.conn.execute('''
                SELECT user_id FROM notification_preferences
                WHERE weekly_digest_enabled = 1
                ORDER BY user_id
            ''').fetchall()
            return [str(row['user_id']) for row in rows]

    async def claim_notification_delivery(self, user_id, kind, period_key):
        async with db_lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT OR IGNORE INTO notification_deliveries
                    (user_id, kind, period_key, status)
                VALUES (?, ?, ?, 'processing')
            ''', (str(user_id), kind, period_key))
            claimed = cursor.rowcount > 0
            if not claimed:
                cursor.execute('''
                    UPDATE notification_deliveries
                    SET status = 'processing', message_id = NULL, error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND kind = ? AND period_key = ?
                      AND (
                          status = 'failed'
                          OR (
                              status = 'processing'
                              AND updated_at < datetime('now', '-15 minutes')
                          )
                      )
                ''', (str(user_id), kind, period_key))
                claimed = cursor.rowcount > 0
            self.conn.commit()
            return claimed

    async def finish_notification_delivery(
        self, user_id, kind, period_key, status, *, message_id=None, error=None
    ):
        async with db_lock:
            self.conn.execute('''
                UPDATE notification_deliveries
                SET status = ?, message_id = ?, error = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND kind = ? AND period_key = ?
            ''', (
                status, message_id, error, str(user_id), kind, period_key,
            ))
            self.conn.commit()

    async def save_category_rename(
        self, user_id, category_type, old_name, new_name, settings,
        *, removed_subcategories=(),
    ):
        """Atomically relabel every registered dependency and settings."""
        owner = str(user_id)
        async with db_lock:
            cursor = self.conn.cursor()
            changed = {}
            try:
                cursor.execute('BEGIN IMMEDIATE')
                for dependency, statement in CATEGORY_RENAME_DEPENDENCY_HOOKS:
                    cursor.execute(
                        statement, (new_name, owner, category_type, old_name)
                    )
                    changed[dependency] = cursor.rowcount
                removed = tuple(dict.fromkeys(removed_subcategories))
                if removed:
                    placeholders = ','.join('?' for _ in removed)
                    cursor.execute(f'''
                        UPDATE recurring_operations
                        SET active = 0, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = ? AND type = ? AND category = ?
                          AND subcategory IN ({placeholders}) AND active = 1
                    ''', (owner, category_type, new_name, *removed))
                    changed['recurring_subcategories_paused'] = cursor.rowcount
                cursor.execute('''
                    INSERT INTO user_settings
                        (user_id, settings_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        settings_json = excluded.settings_json,
                        updated_at = CURRENT_TIMESTAMP
                ''', (owner, json.dumps(settings, ensure_ascii=False)))
                self.conn.commit()
                return changed
            except Exception:
                self.conn.rollback()
                raise

    async def save_subcategory_delete(
        self, user_id, category_type, category, subcategory, settings
    ):
        """Atomically persist settings and pause templates using the removed subcategory."""
        owner = str(user_id)
        async with db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('BEGIN IMMEDIATE')
                cursor.execute('''
                    UPDATE recurring_operations
                    SET active = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND type = ? AND category = ?
                      AND subcategory = ? AND active = 1
                ''', (owner, category_type, category, subcategory))
                paused = cursor.rowcount
                cursor.execute('''
                    INSERT INTO user_settings
                        (user_id, settings_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        settings_json = excluded.settings_json,
                        updated_at = CURRENT_TIMESTAMP
                ''', (owner, json.dumps(settings, ensure_ascii=False)))
                self.conn.commit()
                return {'recurring_subcategories_paused': paused}
            except Exception:
                self.conn.rollback()
                raise

    async def save_category_delete(
        self, user_id, category_type, category, settings
    ):
        """Atomically remove config dependencies but retain financial rows."""
        owner = str(user_id)
        async with db_lock:
            cursor = self.conn.cursor()
            changed = {}
            try:
                cursor.execute('BEGIN IMMEDIATE')
                for dependency, statement in CATEGORY_DELETE_DEPENDENCY_HOOKS:
                    cursor.execute(statement, (owner, category_type, category))
                    changed[dependency] = cursor.rowcount
                cursor.execute('''
                    INSERT INTO user_settings
                        (user_id, settings_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        settings_json = excluded.settings_json,
                        updated_at = CURRENT_TIMESTAMP
                ''', (owner, json.dumps(settings, ensure_ascii=False)))
                self.conn.commit()
                return changed
            except Exception:
                self.conn.rollback()
                raise

    async def save_employee_delete(self, user_id, employee_name, settings):
        """Remove generated config dependencies while retaining history."""
        owner = str(user_id)
        categories = (
            ('income', f'Від {employee_name}'),
            ('expense', f'ЗП {employee_name}'),
        )
        async with db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute('BEGIN IMMEDIATE')
                deleted_budgets = paused_recurring = 0
                for category_type, category in categories:
                    cursor.execute('''
                        DELETE FROM budgets
                        WHERE user_id = ? AND type = ? AND category = ?
                    ''', (owner, category_type, category))
                    deleted_budgets += cursor.rowcount
                    cursor.execute('''
                        UPDATE recurring_operations
                        SET active = 0, updated_at = CURRENT_TIMESTAMP
                        WHERE user_id = ? AND type = ? AND category = ?
                          AND active = 1
                    ''', (owner, category_type, category))
                    paused_recurring += cursor.rowcount
                cursor.execute('''
                    INSERT INTO user_settings
                        (user_id, settings_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET
                        settings_json = excluded.settings_json,
                        updated_at = CURRENT_TIMESTAMP
                ''', (owner, json.dumps(settings, ensure_ascii=False)))
                self.conn.commit()
                return {
                    'budgets': deleted_budgets,
                    'recurring_operations_paused': paused_recurring,
                }
            except Exception:
                self.conn.rollback()
                raise

    async def delete_transaction(self, transaction_id, user_id=None):
        """Delete a transaction. If user_id is given, scope the delete
        to that owner — preferred path from the API. Bot's inline-button
        flows call without user_id for backward compatibility."""
        async with db_lock:
            cursor = self.conn.cursor()
            if user_id is not None:
                cursor.execute(
                    "DELETE FROM transactions WHERE id = ? AND user_id = ?",
                    (transaction_id, str(user_id)),
                )
            else:
                cursor.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            self.conn.commit()
            return cursor.rowcount > 0

    async def delete_time_track(self, track_id, user_id=None):
        """Delete a time track. user_id scopes it to the owner (API path)."""
        async with db_lock:
            cursor = self.conn.cursor()
            if user_id is not None:
                cursor.execute(
                    "DELETE FROM time_tracks WHERE id = ? AND user_id = ?",
                    (track_id, str(user_id)),
                )
            else:
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
    """Save settings to file *atomically*.
    Write to <file>.tmp first, then os.replace — guarantees that no concurrent
    request ever sees a half-written settings.json (which would corrupt on
    next load_settings and silently revert all custom data to DEFAULT_SETTINGS).
    """
    tmp = SETTINGS_FILE + '.tmp'
    os.makedirs(os.path.dirname(tmp) or '.', exist_ok=True)
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


# Load settings and create dynamic categories
SETTINGS = load_settings()
# EMPLOYEES is a *copy* of SETTINGS['employees'] — they MUST be separate
# list objects, otherwise EMPLOYEES.clear() also wipes SETTINGS['employees']
# (Python list aliasing), and POST /api/employees ends up persisting an
# empty list to disk while ostensibly "adding" a new employee.
EMPLOYEES = list(SETTINGS['employees'])
TAX_CONFIG = SETTINGS['tax_config']
CATEGORIES = SETTINGS['categories']
# If settings.json lacks 'time_categories', fall back to a *copy* of
# DEFAULT_SETTINGS['time_categories'] — never an alias, or any subsequent
# mutation would silently corrupt the module-level defaults dict.
TIME_CATEGORIES = SETTINGS.get('time_categories') or dict(DEFAULT_SETTINGS['time_categories'])

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


# ============================================================
# Per-user settings layer.
# The globals above (SETTINGS, CATEGORIES, EMPLOYEES, TAX_CONFIG,
# TIME_CATEGORIES) are now *defaults* used to seed each new user and
# kept as a fallback for the bot's Telegram-flow until that's migrated.
# Every Mini App request runs through user_settings_for(user_id) so
# one user's edits never leak to another's view.
# ============================================================

import copy as _copy


_TAX_GROUPS = {'fop1', 'fop2', 'fop3', 'none'}
_TAX_SCHEMES = {'5_percent', '3_percent_vat'}
_LEGACY_TAX_DEFAULTS = {
    'fop1_fixed': 303.0,
    'fop2_fixed': 1600.0,
    'esv_fixed': 1760.0,
}


def _scheme_from_rate(value) -> str:
    try:
        return '3_percent_vat' if float(value) == 0.03 else '5_percent'
    except (TypeError, ValueError):
        return '5_percent'


def _tax_rules_for_year(year: int) -> dict:
    try:
        normalized_year = int(year)
    except (TypeError, ValueError) as exc:
        raise ValueError('invalid tax rules year') from exc
    rules = TAX_RULES_BY_YEAR.get(normalized_year)
    if rules is None:
        supported = ', '.join(str(y) for y in sorted(TAX_RULES_BY_YEAR))
        raise ValueError(f'tax rules unavailable for {normalized_year}; supported: {supported}')
    return rules


def tax_profile_for_year(tax_config: dict | None, year: int) -> dict:
    """Resolve one report-year profile over versioned official defaults."""
    normalized_year = int(year)
    rules = _tax_rules_for_year(normalized_year)
    config = tax_config if isinstance(tax_config, dict) else {}
    profiles = config.get('profiles_by_year')
    profile = {}
    if isinstance(profiles, dict):
        candidate = profiles.get(str(normalized_year), {})
        if isinstance(candidate, dict):
            profile = candidate

    group = profile.get('group', config.get('group', 'fop3'))
    if group not in _TAX_GROUPS:
        group = 'fop3'

    scheme = profile.get('scheme', config.get('scheme'))
    if scheme not in _TAX_SCHEMES:
        rate_hint = profile.get('single_tax_rate', config.get('single_tax_rate', 0.05))
        scheme = _scheme_from_rate(rate_hint)

    def configured_amount(field):
        value = profile.get(field, config.get(field, rules[field]))
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float(rules[field])
        return number if number >= 0 else float(rules[field])

    return {
        'year': normalized_year,
        'group': group,
        'scheme': scheme,
        'single_tax_rate': 0.03 if scheme == '3_percent_vat' else 0.05,
        'fop1_fixed': configured_amount('fop1_fixed'),
        'fop2_fixed': configured_amount('fop2_fixed'),
        'esv_fixed': configured_amount('esv_fixed'),
        'military_fixed': float(rules['military_fixed']),
        'military_rate': float(rules['military_rate']),
    }


def normalize_tax_config(settings: dict) -> dict:
    """Migrate a user's legacy flat tax settings in memory, idempotently."""
    raw = settings.get('tax_config')
    config = raw if isinstance(raw, dict) else {}
    profiles = config.get('profiles_by_year')
    if not isinstance(profiles, dict):
        profiles = {}

    legacy_group = config.get('group', 'fop3')
    if legacy_group not in _TAX_GROUPS:
        legacy_group = 'fop3'
    legacy_scheme = config.get('scheme')
    if legacy_scheme not in _TAX_SCHEMES:
        legacy_scheme = _scheme_from_rate(config.get('single_tax_rate', 0.05))

    # Snapshot group/scheme for each supported year so future edits cannot
    # silently rewrite an older report.
    for rules_year in TAX_RULES_BY_YEAR:
        candidate = profiles.get(str(rules_year))
        profile = candidate if isinstance(candidate, dict) else {}
        profile.setdefault('group', legacy_group)
        profile.setdefault('scheme', legacy_scheme)
        profiles[str(rules_year)] = profile

    # Preserve customized legacy values for the current year, while replacing
    # the old shipped 2025 defaults with official 2026 values.
    current_profile = profiles[str(CURRENT_TAX_RULES_YEAR)]
    for field, old_default in _LEGACY_TAX_DEFAULTS.items():
        if field not in config:
            continue
        try:
            value = float(config[field])
        except (TypeError, ValueError):
            continue
        if value != old_default:
            current_profile.setdefault(field, value)

    config['profiles_by_year'] = profiles
    config['group'] = current_profile['group']
    config['scheme'] = current_profile['scheme']
    config['note'] = TAX_DISCLAIMER
    for field in ('single_tax_rate', 'fop1_fixed', 'fop2_fixed', 'esv_fixed'):
        config.pop(field, None)
    settings['tax_config'] = config
    return config


def update_tax_profile(settings: dict, year: int, changes: dict) -> dict:
    """Apply validated changes only to the selected report year."""
    normalized_year = int(year)
    _tax_rules_for_year(normalized_year)
    config = normalize_tax_config(settings)
    profile = tax_profile_for_year(config, normalized_year)
    stored = {
        key: profile[key]
        for key in ('group', 'scheme', 'fop1_fixed', 'fop2_fixed', 'esv_fixed')
    }
    stored.update(changes)
    config['profiles_by_year'][str(normalized_year)] = stored
    if normalized_year == CURRENT_TAX_RULES_YEAR:
        config['group'] = stored['group']
        config['scheme'] = stored['scheme']
    return tax_profile_for_year(config, normalized_year)


EMPLOYEE_CATEGORY_PREFIXES = {
    'income': 'Від ',
    'expense': 'ЗП ',
}


def _is_employee_category_namespace(category_type, name):
    prefix = EMPLOYEE_CATEGORY_PREFIXES.get(category_type)
    return bool(prefix and isinstance(name, str) and name.startswith(prefix))


def _employee_categories_dict(employees):
    income_emp = {}
    expense_emp = {}
    for emp in employees:
        income_emp[f'Від {emp}'] = {
            'emoji': '👤',
            'keywords': [emp.lower(), f'від {emp.lower()}'],
        }
        expense_emp[f'ЗП {emp}'] = {
            'emoji': '💼',
            'keywords': [f'зп {emp.lower()}', f'зарплата {emp.lower()}'],
        }
    return income_emp, expense_emp


def employee_names_for_report(settings, transactions):
    """Current employees plus names preserved in historical employee rows."""
    names = list(dict.fromkeys(settings.get('employees', [])))
    seen = set(names)
    historical = set()
    for row in transactions:
        category_type = row.get('type')
        category = row.get('category') or ''
        prefix = EMPLOYEE_CATEGORY_PREFIXES.get(category_type)
        if prefix and category.startswith(prefix):
            employee = category[len(prefix):].strip()
            if employee and employee not in seen:
                historical.add(employee)
    names.extend(sorted(historical, key=str.casefold))
    return names


def rebuild_user_categories(settings):
    """In a per-user settings dict, rebuild auto-generated employee
    categories ('Від <name>' / 'ЗП <name>') from settings['employees'].
    Mutates the dict in place."""
    cats = settings.setdefault('categories',
                               _copy.deepcopy(DEFAULT_SETTINGS['categories']))
    cats.setdefault('income', {})
    cats.setdefault('expense', {})
    cats['income'] = {k: v for k, v in cats['income'].items() if not k.startswith('Від ')}
    cats['expense'] = {k: v for k, v in cats['expense'].items() if not k.startswith('ЗП ')}
    income_emp, expense_emp = _employee_categories_dict(settings.get('employees', []))
    cats['income'].update(income_emp)
    cats['expense'].update(expense_emp)


async def user_settings_for(user_id):
    """Return this user's settings dict, creating a deep-copy of defaults on
    first access. Always returns a complete shape (all DEFAULT_SETTINGS keys
    present, employee-derived categories rebuilt)."""
    existing = await db.get_user_settings(user_id)
    if existing is None:
        existing = _copy.deepcopy(DEFAULT_SETTINGS)
    else:
        for key, default_value in DEFAULT_SETTINGS.items():
            if key not in existing:
                existing[key] = _copy.deepcopy(default_value)
    normalize_tax_config(existing)
    rebuild_user_categories(existing)
    return existing


async def save_user_settings(user_id, settings):
    """Persist the user's settings dict. Also rebuilds employee categories
    so the on-disk copy stays consistent."""
    normalize_tax_config(settings)
    rebuild_user_categories(settings)
    await db.save_user_settings(user_id, settings)


_user_settings_locks = _LoopLocalLockMap()
_account_request_locks = _LoopLocalLockMap()


def serialized_user_settings_write(handler):
    """Serialize read/modify/write handlers, including direct test calls.

    The middleware marks requests for which it already owns the same lock, so
    the decorator is non-reentrant and cannot deadlock in normal API traffic.
    """
    @wraps(handler)
    async def wrapped(request):
        if request.get('_settings_lock_held'):
            return await handler(request)
        owner = str(request['user_id'])
        async with _user_settings_locks[owner]:
            async with _recurring_user_locks[owner]:
                request['_settings_lock_held'] = True
                try:
                    return await handler(request)
                finally:
                    request.pop('_settings_lock_held', None)
    return wrapped


def serialized_recurring_write(handler):
    @wraps(handler)
    async def wrapped(request):
        owner = str(request['user_id'])
        async with _recurring_user_locks[owner]:
            return await handler(request)
    return wrapped


async def update_user_settings(user_id, mutator):
    """Atomically apply a synchronous mutation to one user's settings."""
    lock = _user_settings_locks[str(user_id)]
    async with lock:
        settings = await user_settings_for(user_id)
        result = mutator(settings)
        await save_user_settings(user_id, settings)
        return settings if result is None else result


async def _delete_employee_locked(user_id, employee_name):
    settings = _copy.deepcopy(await user_settings_for(user_id))
    employees = settings.setdefault('employees', [])
    if employee_name not in employees:
        return False
    employees.remove(employee_name)
    normalize_tax_config(settings)
    rebuild_user_categories(settings)
    await db.save_employee_delete(user_id, employee_name, settings)
    return True


async def delete_employee_for_user(user_id, employee_name):
    """Dependency-aware employee deletion shared by API and bot chat flows."""
    owner = str(user_id)
    async with _user_settings_locks[owner]:
        async with _recurring_user_locks[owner]:
            return await _delete_employee_locked(owner, employee_name)


async def delete_category_for_user(user_id, category_type, category):
    owner = str(user_id)
    async with _user_settings_locks[owner]:
        async with _recurring_user_locks[owner]:
            if _is_employee_category_namespace(category_type, category):
                return False
            settings = _copy.deepcopy(await user_settings_for(owner))
            bucket = settings.get('categories', {}).get(category_type, {})
            if category not in bucket or category == 'Інше':
                return False
            del bucket[category]
            normalize_tax_config(settings)
            rebuild_user_categories(settings)
            await db.save_category_delete(
                owner, category_type, category, settings
            )
            return True


async def reset_user_configuration(user_id):
    """Reset settings/budgets and pause recurring templates under one lock."""
    owner = str(user_id)
    async with _user_settings_locks[owner]:
        async with _recurring_user_locks[owner]:
            return await db.reset_user_settings(owner)


# ========== EXCHANGE RATES ==========
async def update_exchange_rates():
    """Atomically replace the cache only with a complete verified NBU snapshot."""
    global exchange_rates_cache

    try:
        async with aiohttp.ClientSession() as session:
            url = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"

            async with session.get(url, timeout=5) as response:
                if response.status != 200:
                    raise ExchangeRateUnavailableError('NBU returned non-success status')
                data = await response.json()
        candidates = {}
        for item in data if isinstance(data, list) else ():
            currency = item.get('cc') if isinstance(item, dict) else None
            if currency not in {'USD', 'EUR'}:
                continue
            value = float(item.get('rate'))
            if not math.isfinite(value) or value <= 0:
                raise ExchangeRateUnavailableError('NBU returned an invalid rate')
            candidates[currency] = value
        if set(candidates) != {'USD', 'EUR'}:
            raise ExchangeRateUnavailableError('NBU response is incomplete')
        exchange_rates_cache = {
            'USD': candidates['USD'],
            'EUR': candidates['EUR'],
            'last_update': datetime.now(KYIV_TZ),
        }
        logger.info('Exchange rates updated from NBU')
        return True
    except Exception as exc:
        logger.warning('Exchange rate refresh failed: %s', type(exc).__name__)
        return False


async def get_exchange_rate(currency):
    """Return a verified current or stale last-good rate."""
    global exchange_rates_cache

    if currency == 'UAH':
        return 1.0

    last_update = exchange_rates_cache.get('last_update')
    if not last_update or (datetime.now(KYIV_TZ) - last_update).total_seconds() > 1800:
        await update_exchange_rates()

    rate = exchange_rates_cache.get(currency)
    if not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate <= 0:
        raise ExchangeRateUnavailableError(
            f'No verified exchange rate is available for {currency}'
        )
    return float(rate)


def convert_to_uah(amount, currency, rate):
    """Convert amount to UAH"""
    if currency == 'UAH':
        return amount
    return amount * rate


# ========== UTILITY FUNCTIONS ==========
MAX_TRANSACTION_AMOUNT = Decimal('1000000000')
CALLBACK_REF_PREFIX = '~'


def _callback_ref(value):
    """Return a deterministic, Telegram-safe opaque reference for user text."""
    digest = hashlib.blake2s(str(value).encode('utf-8'), digest_size=9).hexdigest()
    return f'{CALLBACK_REF_PREFIX}{digest}'


def _resolve_callback_ref(raw_value, candidates):
    """Resolve current opaque callbacks while accepting legacy/raw callback text."""
    raw = str(raw_value)
    available = tuple(str(candidate) for candidate in candidates)
    if raw.startswith(CALLBACK_REF_PREFIX):
        matches = tuple(value for value in available if _callback_ref(value) == raw)
        if len(matches) == 1:
            return matches[0]
        return raw if not matches and raw in available else None
    if raw in available:
        return raw
    # Buttons created before this release replaced colons with this sentinel.
    legacy = raw.replace('_COLON_', ':')
    return legacy if legacy in available else None


def _parse_positive_money(value):
    """Parse the shared bot/API money domain without float overflow surprises."""
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or not 0 < amount <= MAX_TRANSACTION_AMOUNT:
        return None
    return float(amount)


def parse_transaction(text, categories_by_type=None):
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

    amount_text = text.strip()
    if amount_text.startswith(('+', '-')):
        amount_text = amount_text[1:].lstrip()
    amount_match = re.search(
        r'(?<![\w.,])(\d+(?:[.,]\d{1,2})?)(?![\w.,])', amount_text
    )
    if not amount_match:
        return None

    amount_str = amount_match.group(1).replace(',', '.')
    amount = _parse_positive_money(amount_str)
    if amount is None:
        return None

    currency = 'UAH'
    if any(word in text_lower for word in ['usd', 'долар', 'доллар', '$']):
        currency = 'USD'
    elif any(word in text_lower for word in ['eur', 'євро', 'euro', '€']):
        currency = 'EUR'

    category = 'Інше'
    for cat_name, cat_data in (categories_by_type or CATEGORIES)[trans_type].items():
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
    match = re.match(r'^(\d+)\s*(?:хв|м|min|minute|minutes)?$', text)
    if match:
        return int(match.group(1))
    
    # Pattern: "1.5год" or "2h"
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(?:год|h|hour|hours)$', text)
    if match:
        minutes = Decimal(match.group(1)) * 60
        return int(minutes) if minutes == minutes.to_integral_value() else None
    
    # Pattern: "2год 30хв" or "2h 30m"
    match = re.match(r'^(\d+)\s*(?:год|h)\s+(\d+)\s*(?:хв|м|min)?$', text)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        return hours * 60 + minutes
    
    # Just a number
    try:
        return int(text)
    except (TypeError, ValueError):
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


def get_time_category_keyboard(time_categories=None):
    """Create time category selection keyboard"""
    keyboard = []
    
    # Group in rows of 2
    row = []
    for cat_name, cat_data in (time_categories or TIME_CATEGORIES).items():
        emoji = cat_data['emoji']
        button = InlineKeyboardButton(
            f"{emoji} {cat_name}",
            callback_data=f"timecat:{_callback_ref(cat_name)}"
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
    category_ref = _callback_ref(category)
    keyboard = [
        [InlineKeyboardButton("UAH ₴", callback_data=f"curr:{trans_type}:{category_ref}:UAH")],
        [InlineKeyboardButton("USD $", callback_data=f"curr:{trans_type}:{category_ref}:USD")],
        [InlineKeyboardButton("EUR €", callback_data=f"curr:{trans_type}:{category_ref}:EUR")],
        [InlineKeyboardButton("◀️ Назад", callback_data=f"type:{trans_type}")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_category_keyboard(trans_type, categories_by_type=None):
    """Create category selection keyboard"""
    keyboard = []
    categories = (categories_by_type or CATEGORIES)[trans_type]

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
                callback_data=f"cat:expense:{_callback_ref(cat_name)}"
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
                callback_data=f"cat:income:{_callback_ref(cat_name)}"
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


def get_salary_submenu_keyboard(employees=None):
    """Create salary payment submenu"""
    keyboard = []
    for emp in (employees if employees is not None else EMPLOYEES):
        keyboard.append([InlineKeyboardButton(
            f"💼 {emp}",
            callback_data=f"cat:expense:{_callback_ref(f'ЗП {emp}')}"
        )])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="type:expense")])
    return InlineKeyboardMarkup(keyboard)


def get_employee_income_submenu_keyboard(employees=None):
    """Create employee income submenu"""
    keyboard = []
    for emp in (employees if employees is not None else EMPLOYEES):
        keyboard.append([InlineKeyboardButton(
            f"👤 {emp}",
            callback_data=f"cat:income:{_callback_ref(f'Від {emp}')}"
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


def get_time_category_list_keyboard(time_categories=None):
    """Create time category list keyboard for settings"""
    keyboard = []
    
    for cat_name, cat_data in (time_categories or TIME_CATEGORIES).items():
        emoji = cat_data['emoji']
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {cat_name}", callback_data=f"timecatview:{_callback_ref(cat_name)}"),
            InlineKeyboardButton("❌", callback_data=f"timecatdel:{_callback_ref(cat_name)}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Додати категорію", callback_data="timecatadd")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_employee_list_keyboard(employees=None):
    """Create employee list keyboard"""
    keyboard = []
    for emp in (employees if employees is not None else EMPLOYEES):
        keyboard.append([
            InlineKeyboardButton(f"👤 {emp}", callback_data=f"emp_view:{_callback_ref(emp)}"),
            InlineKeyboardButton("❌", callback_data=f"emp_del:{_callback_ref(emp)}")
        ])
    keyboard.append([InlineKeyboardButton("➕ Додати працівника", callback_data="emp_add")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="settings:main")])
    return InlineKeyboardMarkup(keyboard)


def get_category_list_keyboard(cat_type, categories_by_type=None):
    """Create category list keyboard"""
    keyboard = []
    categories = (categories_by_type or CATEGORIES)[cat_type]

    # Filter out employee categories
    if cat_type == 'expense':
        cats_list = [k for k in categories.keys() if not k.startswith('ЗП ')]
    else:
        cats_list = [k for k in categories.keys() if not k.startswith('Від ')]

    for cat_name in cats_list:
        cat_data = categories[cat_name]
        emoji = cat_data['emoji']
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {cat_name}", callback_data=f"catview:{cat_type}:{_callback_ref(cat_name)}"),
            InlineKeyboardButton("❌", callback_data=f"catdel:{cat_type}:{_callback_ref(cat_name)}")
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
    category_ref = _callback_ref(category)

    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=f"num:{trans_type}:{category_ref}:{currency}:1"),
            InlineKeyboardButton("2", callback_data=f"num:{trans_type}:{category_ref}:{currency}:2"),
            InlineKeyboardButton("3", callback_data=f"num:{trans_type}:{category_ref}:{currency}:3")
        ],
        [
            InlineKeyboardButton("4", callback_data=f"num:{trans_type}:{category_ref}:{currency}:4"),
            InlineKeyboardButton("5", callback_data=f"num:{trans_type}:{category_ref}:{currency}:5"),
            InlineKeyboardButton("6", callback_data=f"num:{trans_type}:{category_ref}:{currency}:6")
        ],
        [
            InlineKeyboardButton("7", callback_data=f"num:{trans_type}:{category_ref}:{currency}:7"),
            InlineKeyboardButton("8", callback_data=f"num:{trans_type}:{category_ref}:{currency}:8"),
            InlineKeyboardButton("9", callback_data=f"num:{trans_type}:{category_ref}:{currency}:9")
        ],
        [
            InlineKeyboardButton("⌫", callback_data=f"num:{trans_type}:{category_ref}:{currency}:back"),
            InlineKeyboardButton("0", callback_data=f"num:{trans_type}:{category_ref}:{currency}:0"),
            InlineKeyboardButton(".", callback_data=f"num:{trans_type}:{category_ref}:{currency}:dot")
        ],
        [
            InlineKeyboardButton("✅ Підтвердити", callback_data=f"num:{trans_type}:{category_ref}:{currency}:confirm"),
        ],
        [
            InlineKeyboardButton("◀️ Назад", callback_data=f"cat:{trans_type}:{category_ref}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def generate_text_chart(data_dict, total, title, categories_by_type=None):
    """Generate text-based chart"""
    if not data_dict or total == 0:
        return "Немає даних"

    text = f"📊 {title}\n\n"

    sorted_items = sorted(data_dict.items(), key=lambda x: x[1], reverse=True)

    for category, amount in sorted_items:
        percentage = (amount / total) * 100
        emoji = '📦'
        for cat_type in ['income', 'expense']:
            categories = categories_by_type or CATEGORIES
            if category in categories[cat_type]:
                emoji = categories[cat_type][category]['emoji']
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
def _open_app_keyboard():
    """A single large button that opens the Mini App directly."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💎 Відкрити Ruby Finance",
            web_app=WebAppInfo(url=_miniapp_public_url()),
        )
    ]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command — Mini App-first welcome (no legacy menu/commands)."""
    await db.upsert_user(update.effective_user)
    await update.message.reply_text(
        "👋 Вітаємо у Ruby Finance!\n\n"
        "Це ваш особистий застосунок для обліку доходів, витрат, часу "
        "та податків ФОП — усе зручно й наочно в одному місці.\n\n"
        "Натисніть кнопку нижче, щоб відкрити застосунок 👇",
        reply_markup=_open_app_keyboard(),
    )


async def show_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show info — Mini App-first."""
    await update.message.reply_text(
        f"ℹ️ Ruby Finance {_bot_handle()}\n\n"
        "Усі функції — у застосунку:\n"
        "• облік доходів, витрат і часу\n"
        "• категорії та підкатегорії\n"
        "• звіти, діаграми, ROI працівників\n"
        "• податки для ФОП (2026), бюджети, регулярні операції\n"
        "• розумні підказки, тижневі підсумки, прогноз\n\n"
        "🔒 Приватність: дані кожного користувача ізольовані за Telegram ID; "
        "резервні копії зберігаються окремо й перевіряються. "
        "Політика приватності — /privacy, повне видалення даних — /clear.\n\n"
        "Натисніть кнопку нижче, щоб відкрити застосунок 👇",
        reply_markup=_open_app_keyboard(),
    )


def _miniapp_public_url(path=''):
    base = os.getenv(
        'MINIAPP_PUBLIC_URL',
        'https://finance-bot-production-5de8.up.railway.app',
    ).rstrip('/')
    return f'{base}/{path.lstrip("/")}'


async def privacy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '🔒 Політика приватності Ruby Finance:\n'
        f'{_miniapp_public_url("privacy")}\n\n'
        'Для повного видалення облікового запису використайте /clear.'
    )


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '📄 Умови використання Ruby Finance:\n'
        f'{_miniapp_public_url("terms")}'
    )


async def clear_account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('ВИДАЛИТИ ВСІ МОЇ ДАНІ', callback_data='account_delete:confirm'),
    ], [
        InlineKeyboardButton('Скасувати', callback_data='cancel'),
    ]])
    await update.message.reply_text(
        '⚠️ Повністю видалити транзакції, записи часу, налаштування та профіль?\n\n'
        'Цю дію неможливо скасувати.',
        reply_markup=keyboard,
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
    settings = await user_settings_for(user_id)

    transactions = await db.get_transactions(user_id, limit=1)

    if not transactions:
        await update.message.reply_text(
            "📭 Немає транзакцій для відміни.",
            reply_markup=get_main_keyboard()
        )
        return

    last = transactions[0]

    emoji = "💸" if last['type'] == 'expense' else "💰"
    cat_emoji = settings['categories'][last['type']].get(last['category'], {}).get('emoji', '📦')

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
    settings = await user_settings_for(user_id)
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
        cat_emoji = settings['categories'][t['type']].get(t['category'], {}).get('emoji', '📦')

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
async def _handle_feedback_callback(query, context):
    """Announcement buttons: record a per-feature 👍/👎, or start the free-text
    feedback flow. Owns its answer()/toast, so it must run before the generic ack."""
    parts = (query.data or '').split(':')
    user_id = str(query.from_user.id)
    now = datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S')
    if len(parts) >= 2 and parts[1] == 'comment':
        context.user_data['waiting_for'] = 'feedback_comment'
        await query.answer()
        try:
            await query.message.reply_text(
                '💬 Напишіть ваш відгук або ідею одним повідомленням 👇'
            )
        except Exception:
            pass
        return
    if len(parts) >= 3 and parts[2] in ('up', 'down'):
        feature, reaction = parts[1], parts[2]
        try:
            await db.record_feature_reaction(user_id, feature, reaction, now)
        except Exception as e:
            logger.warning(f'feedback reaction save failed: {e}')
        label = FEEDBACK_FEATURE_LABELS.get(feature, feature)
        emoji = '👍' if reaction == 'up' else '👎'
        await query.answer(
            f'{emoji} Ваш голос за «{label}» враховано. Дякуємо!',
            show_alert=False,
        )
        return
    await query.answer()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries"""
    query = update.callback_query
    if (query.data or '').startswith('fb:'):
        await _handle_feedback_callback(query, context)
        return
    await query.answer()

    data_parts = query.data.split(':')
    action = data_parts[0]
    user_id = str(query.from_user.id)

    if action == 'admin_broadcast':
        if not is_admin(user_id):
            await query.edit_message_text('⛔ Дія доступна лише адміністратору.')
            return
        mode = data_parts[1] if len(data_parts) > 1 else ''
        token = data_parts[2] if len(data_parts) > 2 else ''
        preview = _broadcast_confirmations.get(token)
        valid_preview = bool(
            preview
            and preview.get('admin_id') == user_id
            and float(preview.get('expires_at', 0)) > datetime.now(timezone.utc).timestamp()
        )
        if not valid_preview:
            await query.edit_message_text('Термін підтвердження минув. Створіть новий preview.')
            return
        if mode == 'cancel':
            _broadcast_confirmations.pop(token, None)
            await query.edit_message_text('Розсилку скасовано.')
            return
        if mode != 'confirm':
            await query.edit_message_text('Невідома дія розсилки.')
            return
        decision = _broadcast_limiter.check(user_id)
        if not decision.allowed:
            await query.edit_message_text(
                f'Зачекайте {decision.retry_after} с перед наступною розсилкою.'
            )
            return
        recipient_ids = tuple(preview.get('recipient_ids') or ())
        message_text = _consume_chat_broadcast_confirmation(user_id, token)
        if not message_text:
            await query.edit_message_text('Підтвердження вже використано або прострочене.')
            return
        await query.edit_message_text('Надсилаю підтверджену розсилку…')
        result = await _send_broadcast_with_bot(
            user_id,
            message_text,
            context.bot,
            recipient_ids=recipient_ids,
        )
        await query.edit_message_text(
            '📣 Розсилка завершена. '
            f"Надіслано: {result['sent']}, помилок: {result['failed']}, "
            f"пропущено: {result['skipped']}."
        )
        return

    user_settings = await user_settings_for(user_id)
    user_categories = user_settings['categories']
    user_employees = user_settings['employees']
    user_time_categories = user_settings['time_categories']
    user_tax_config = tax_profile_for_year(
        user_settings['tax_config'], CURRENT_TAX_RULES_YEAR)

    if action == "cancel":
        await query.edit_message_text("❌ Скасовано")
        return

    elif action == "time":
        # Handle time tracking
        if data_parts[1] == "select":
            await query.edit_message_text(
                "⏱️ **ЗАТРАЧЕНИЙ ЧАС**\n\nОберіть категорію:",
                reply_markup=get_time_category_keyboard(user_time_categories),
                parse_mode='Markdown'
            )
        return

    elif action == "timecat":
        # Time category selected
        category = _resolve_callback_ref(
            ':'.join(data_parts[1:]), user_time_categories
        )
        if category is None:
            await query.edit_message_text(
                "⚠️ Ця кнопка застаріла. Відкрийте меню додавання ще раз."
            )
            return
        context.user_data['waiting_for'] = f'time_minutes:{category}'
        
        cat_emoji = user_time_categories.get(category, {}).get('emoji', '⏱️')
        
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
            cat_emoji = user_categories[last['type']].get(last['category'], {}).get('emoji', '📦')

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
        deleted = await db.delete_transaction(transaction_id, user_id=user_id)

        if deleted:
            await query.edit_message_text("✅ Транзакцію видалено!")
        else:
            await query.edit_message_text("❌ Помилка видалення")
        return

    elif action == "account_delete":
        if len(data_parts) < 2 or data_parts[1] != 'confirm':
            await query.edit_message_text('❌ Невірне підтвердження.')
            return
        async with _account_request_locks[user_id]:
            await db.delete_user_account(user_id)
        await query.edit_message_text(
            '✅ Ваш обліковий запис і всі пов’язані фінансові дані видалено.'
        )
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

    elif action == "timecatview":
        cat_name = _resolve_callback_ref(
            ':'.join(data_parts[1:]), user_time_categories
        )
        if cat_name is None:
            await query.edit_message_text("⚠️ Категорію часу не знайдено.")
            return
        emoji = user_time_categories[cat_name].get('emoji', '⏱️')
        await query.edit_message_text(
            f"{emoji} Категорія часу\n\nНазва: {cat_name}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "◀️ Назад", callback_data="settings:time_cats"
                )
            ]]),
        )
        return

    elif action == "timecatdel":
        cat_name = _resolve_callback_ref(
            ':'.join(data_parts[1:]), user_time_categories
        )
        
        if cat_name is not None and cat_name != 'Інше':
            await update_user_settings(
                user_id, lambda s: s['time_categories'].pop(cat_name, None))
            
            await query.edit_message_text(
                f"✅ Категорію \"{cat_name}\" видалено!",
                reply_markup=get_time_category_list_keyboard(
                    (await user_settings_for(user_id))['time_categories'])
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

    elif action == "emp_view":
        emp_name = _resolve_callback_ref(
            ':'.join(data_parts[1:]), user_employees
        )
        if emp_name is None:
            await query.edit_message_text("⚠️ Працівника не знайдено.")
            return
        await query.edit_message_text(
            f"👤 Працівник\n\nІм'я: {emp_name}\n"
            f"Дохідна категорія: Від {emp_name}\n"
            f"Витратна категорія: ЗП {emp_name}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "◀️ Назад", callback_data="settings:employees"
                )
            ]]),
        )
        return

    elif action == "emp_del":
        emp_name = _resolve_callback_ref(
            ':'.join(data_parts[1:]), user_employees
        )
        if emp_name is not None:
            await delete_employee_for_user(user_id, emp_name)

            await query.edit_message_text(
                f"✅ Працівника \"{emp_name}\" видалено!",
                reply_markup=get_employee_list_keyboard(
                    (await user_settings_for(user_id))['employees'])
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

    elif action == "catview":
        cat_type = data_parts[1] if len(data_parts) > 1 else ''
        categories = user_categories.get(cat_type, {})
        cat_name = _resolve_callback_ref(
            ':'.join(data_parts[2:]), categories
        )
        if cat_name is None:
            await query.edit_message_text("⚠️ Категорію не знайдено.")
            return
        cat_data = categories[cat_name]
        cat_type_name = "витрат" if cat_type == "expense" else "доходів"
        keywords = ', '.join(cat_data.get('keywords') or []) or 'немає'
        subcategories = ', '.join(cat_data.get('subcategories') or []) or 'немає'
        await query.edit_message_text(
            f"{cat_data.get('emoji', '📦')} Категорія {cat_type_name}\n\n"
            f"Назва: {cat_name}\n"
            f"Ключові слова: {keywords}\n"
            f"Підрозділи: {subcategories}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "◀️ Назад",
                    callback_data=(
                        "settings:expense_cats"
                        if cat_type == "expense"
                        else "settings:income_cats"
                    ),
                )
            ]]),
        )
        return

    elif action == "catdel":
        cat_type = data_parts[1] if len(data_parts) > 1 else ''
        categories = user_categories.get(cat_type, {})
        cat_name = _resolve_callback_ref(
            ':'.join(data_parts[2:]), categories
        )

        if (
            cat_name is not None
            and cat_name != 'Інше'
            and not _is_employee_category_namespace(cat_type, cat_name)
        ):
            await delete_category_for_user(user_id, cat_type, cat_name)

            cat_type_name = "витрат" if cat_type == "expense" else "доходів"
            await query.edit_message_text(
                f"✅ Категорію \"{cat_name}\" видалено!",
                reply_markup=get_category_list_keyboard(
                    cat_type, (await user_settings_for(user_id))['categories'])
            )
        return

    elif action == "tax_edit":
        tax_type = data_parts[1]
        context.user_data['waiting_for'] = f'tax_value:{tax_type}'

        if tax_type == "single_tax":
            current = user_tax_config['single_tax_rate'] * 100
            await query.edit_message_text(
                f"📝 **Зміна ставки єдиного податку**\n\n"
                f"Поточна ставка: {current:.0f}%\n\n"
                f"Надішліть 5 (без ПДВ) або 3 (платник ПДВ)",
                parse_mode='Markdown'
            )
        elif tax_type == "esv":
            current = user_tax_config['esv_fixed']
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
            reply_markup=get_category_keyboard(trans_type, user_categories)
        )

    elif action == "submenu":
        submenu_type = data_parts[1]
        if submenu_type == "salary":
            await query.edit_message_text(
                "💼 ЗП працівникам\n\nОберіть працівника:",
                reply_markup=get_salary_submenu_keyboard(user_employees)
            )
        elif submenu_type == "employees":
            await query.edit_message_text(
                "👥 Від працівників\n\nОберіть працівника:",
                reply_markup=get_employee_income_submenu_keyboard(user_employees)
            )

    elif action == "cat":
        trans_type = data_parts[1] if len(data_parts) > 1 else ''
        categories = user_categories.get(trans_type, {})
        category = _resolve_callback_ref(
            ':'.join(data_parts[2:]), categories
        )
        if category is None:
            await query.edit_message_text(
                "⚠️ Ця категорія більше недоступна. Оберіть її знову."
            )
            return

        context.user_data['trans_type'] = trans_type
        context.user_data['category'] = category

        cat_data = user_categories[trans_type].get(category, {'emoji': '📦'})
        emoji = cat_data['emoji']

        await query.edit_message_text(
            f"{emoji} {category}\n\nОберіть валюту:",
            reply_markup=get_currency_keyboard(trans_type, category)
        )

    elif action == "curr":
        trans_type = data_parts[1] if len(data_parts) > 1 else ''
        currency = data_parts[-1] if len(data_parts) > 2 else ''
        categories = user_categories.get(trans_type, {})
        category = _resolve_callback_ref(
            ':'.join(data_parts[2:-1]), categories
        )
        if category is None or currency not in ('UAH', 'USD', 'EUR'):
            await query.edit_message_text(
                "⚠️ Ця кнопка застаріла. Оберіть категорію та валюту знову."
            )
            return

        context.user_data['trans_type'] = trans_type
        context.user_data['category'] = category
        context.user_data['currency'] = currency
        context.user_data['amount'] = ""

        cat_data = user_categories[trans_type].get(category, {'emoji': '📦'})
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
    settings = await user_settings_for(query.from_user.id)
    employees = settings['employees']
    categories = settings['categories']
    time_categories = settings['time_categories']
    tax_config = settings['tax_config']
    tax_profile = tax_profile_for_year(tax_config, CURRENT_TAX_RULES_YEAR)

    if setting_type == "main":
        await query.edit_message_text(
            "⚙️ **НАЛАШТУВАННЯ**\n\nОберіть розділ:",
            reply_markup=get_settings_keyboard(),
            parse_mode='Markdown'
        )

    elif setting_type == "employees":
        text = "👥 **ПРАЦІВНИКИ**\n\n"
        text += "Поточний список:\n"
        for i, emp in enumerate(employees, 1):
            text += f"{i}. {emp}\n"
        text += "\nНатисніть ❌ щоб видалити або ➕ щоб додати"

        await query.edit_message_text(
            text,
            reply_markup=get_employee_list_keyboard(employees),
            parse_mode='Markdown'
        )

    elif setting_type == "expense_cats":
        text = "📋 **КАТЕГОРІЇ ВИТРАТ**\n\n"
        text += "Натисніть на категорію для перегляду або ❌ для видалення\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_category_list_keyboard('expense', categories),
            parse_mode='Markdown'
        )

    elif setting_type == "income_cats":
        text = "💰 **КАТЕГОРІЇ ДОХОДІВ**\n\n"
        text += "Натисніть на категорію для перегляду або ❌ для видалення\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_category_list_keyboard('income', categories),
            parse_mode='Markdown'
        )

    elif setting_type == "time_cats":
        text = "⏱️ **КАТЕГОРІЇ ЧАСУ**\n\n"
        text += "Натисніть на категорію для перегляду або ❌ для видалення\n\n"

        await query.edit_message_text(
            text,
            reply_markup=get_time_category_list_keyboard(time_categories),
            parse_mode='Markdown'
        )

    elif setting_type == "tax":
        text = f"📊 **ПОДАТКОВІ НАЛАШТУВАННЯ**\n\n"
        text += f"Рік правил: {CURRENT_TAX_RULES_YEAR}\n"
        text += f"Єдиний податок: {tax_profile['single_tax_rate']*100:.0f}%\n"
        text += f"ЄСВ (фіксований): {tax_profile['esv_fixed']:.2f} грн\n\n"
        text += f"💡 {TAX_DISCLAIMER}\n\n"
        text += "Натисніть кнопку для зміни"

        await query.edit_message_text(
            text,
            reply_markup=get_tax_settings_keyboard(),
            parse_mode='Markdown'
        )


async def handle_numpad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle numpad button presses"""
    query = update.callback_query
    settings = await user_settings_for(query.from_user.id)

    parts = query.data.split(':')
    trans_type = parts[1] if len(parts) > 1 else ''
    currency = parts[-2] if len(parts) > 3 else ''
    action = parts[-1] if len(parts) > 2 else ''
    categories = settings['categories'].get(trans_type, {})
    category = _resolve_callback_ref(':'.join(parts[2:-2]), categories)
    if (
        category is None
        or currency not in ('UAH', 'USD', 'EUR')
        or action not in {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
                          'back', 'dot', 'confirm'}
    ):
        await query.answer(
            "⚠️ Ця кнопка застаріла. Почніть введення суми знову.",
            show_alert=True,
        )
        return

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
    cat_data = settings['categories'][trans_type].get(category, {'emoji': '📦'})
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
    settings = await user_settings_for(user_id)

    amount = _parse_positive_money(amount_str)
    category_entry = settings.get('categories', {}).get(trans_type, {}).get(category)
    if (
        amount is None
        or currency not in ('UAH', 'USD', 'EUR')
        or not isinstance(category_entry, dict)
    ):
        await query.answer("❌ Невірна сума!", show_alert=True)
        return

    try:
        rate = await get_exchange_rate(currency)
    except ExchangeRateUnavailableError:
        await query.answer(
            "⚠️ Курс валют тимчасово недоступний. Спробуйте пізніше.",
            show_alert=True,
        )
        return
    amount_uah = round(convert_to_uah(amount, currency, rate), 2)
    if not math.isfinite(amount_uah) or amount_uah < 0.01:
        await query.answer("❌ Сума надто мала або некоректна!", show_alert=True)
        return

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
    cat_emoji = category_entry.get('emoji', '📦')

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
    user_id = str(update.effective_user.id)

    if waiting_for == 'feedback_comment':
        context.user_data['waiting_for'] = None
        comment = (update.message.text or '').strip()
        if comment:
            try:
                await db.add_feature_comment(
                    user_id, comment,
                    datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S'),
                )
            except Exception as e:
                logger.warning(f'feedback comment save failed: {e}')
        await update.message.reply_text(
            'Дякуємо за відгук! Ми обов’язково його врахуємо ❤️'
        )
        return

    user_settings = await user_settings_for(user_id)
    user_categories = user_settings['categories']
    user_employees = user_settings['employees']
    user_time_categories = user_settings['time_categories']

    # Handle time input
    if waiting_for and waiting_for.startswith('time_minutes:'):
        category = waiting_for.replace('time_minutes:', '')
        text = update.message.text.strip()
        
        minutes = parse_time_input(text)
        
        if minutes is None or not 1 <= minutes <= 24 * 60:
            await update.message.reply_text(
                "❌ Не зрозумів. Спробуйте:\n"
                "• `90` — 90 хвилин\n"
                "• `1.5год` — 1.5 години\n"
                "• `2h 30m` — 2 год 30 хв\n"
                "Максимум за один запис — 1440 хвилин.",
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
        
        cat_emoji = user_time_categories.get(category, {}).get('emoji', '⏱️')
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
        
        if not cat_name or len(cat_name) > 60:
            await update.message.reply_text(
                "❌ Назва має містити від 1 до 60 символів. Спробуйте ще раз."
            )
            return

        if cat_name in user_time_categories:
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
        
        await update_user_settings(
            user_id, lambda s: s['time_categories'].update({cat_name: {'emoji': emoji}}))
        
        context.user_data['waiting_for'] = None
        context.user_data['new_time_category'] = None
        
        await update.message.reply_text(
            f"✅ Категорію \"{emoji} {cat_name}\" додано!",
            reply_markup=get_main_keyboard()
        )
        return

    if waiting_for == 'employee_name':
        emp_name = update.message.text.strip()

        if not emp_name or len(emp_name) > 60:
            await update.message.reply_text(
                "❌ Ім'я має містити від 1 до 60 символів. Спробуйте ще раз."
            )
            return

        if emp_name in user_employees:
            await update.message.reply_text(f"⚠️ Працівник \"{emp_name}\" вже існує!")
            return

        await update_user_settings(user_id, lambda s: s['employees'].append(emp_name))

        context.user_data['waiting_for'] = None

        await update.message.reply_text(
            f"✅ Працівника \"{emp_name}\" додано!",
            reply_markup=get_main_keyboard()
        )
        return

    elif waiting_for and waiting_for.startswith('category_name:'):
        cat_type = waiting_for.split(':')[1]
        cat_name = update.message.text.strip()

        if not cat_name or len(cat_name) > 80:
            await update.message.reply_text(
                "❌ Назва має містити від 1 до 80 символів. Спробуйте ще раз."
            )
            return

        if _is_employee_category_namespace(cat_type, cat_name):
            await update.message.reply_text(
                "❌ Ця назва зарезервована для автоматичних категорій працівників."
            )
            return

        if cat_name in user_categories[cat_type]:
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

        await update_user_settings(
            user_id,
            lambda s: s['categories'][cat_type].update({cat_name: {
                'emoji': emoji, 'keywords': keywords, 'subcategories': []}}),
        )

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
            if value not in (3, 5):
                await update.message.reply_text("❌ Введіть 5 (без ПДВ) або 3 (з ПДВ)")
                return

            await update_user_settings(
                user_id,
                lambda s: update_tax_profile(
                    s,
                    CURRENT_TAX_RULES_YEAR,
                    {'scheme': '3_percent_vat' if value == 3 else '5_percent'},
                ),
            )

            await update.message.reply_text(
                f"✅ Ставку єдиного податку змінено на {value:.0f}%",
                reply_markup=get_main_keyboard()
            )

        elif tax_type == "esv":
            if value < 500 or value > 10000:
                await update.message.reply_text("❌ Сума має бути від 500 до 10000 грн")
                return

            await update_user_settings(
                user_id,
                lambda s: update_tax_profile(
                    s, CURRENT_TAX_RULES_YEAR, {'esv_fixed': value}),
            )

            await update.message.reply_text(
                f"✅ Фіксований ЄСВ змінено на {value:.0f} грн",
                reply_markup=get_main_keyboard()
            )

        context.user_data['waiting_for'] = None
        return

    # Try to parse as transaction
    text = update.message.text
    transaction = parse_transaction(text, user_categories)

    if transaction:
        try:
            rate = await get_exchange_rate(transaction['currency'])
        except ExchangeRateUnavailableError:
            await update.message.reply_text(
                "⚠️ Курс валют тимчасово недоступний. Спробуйте пізніше."
            )
            return
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
        cat_data = user_categories[transaction['type']].get(
            transaction['category'], {'emoji': '📦'})
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
    settings = await user_settings_for(user_id)

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
        emoji = settings['time_categories'].get(cat, {}).get('emoji', '⏱️')
        
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
    settings = await user_settings_for(user_id)

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
            emoji = settings['categories']['income'].get(cat[0], {}).get('emoji', '📦')
            text += f"  {emoji} {cat[0]}: {cat[1]:.2f} грн\n"
        text += f"  💰 Разом: {total_income:.2f} грн\n\n"

    if expenses_by_cat:
        text += "💸 **Витрати:**\n"
        for cat in sorted(expenses_by_cat.items(), key=lambda x: x[1], reverse=True):
            emoji = settings['categories']['expense'].get(cat[0], {}).get('emoji', '📦')
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
    settings = await user_settings_for(user_id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    if not transactions:
        await query.edit_message_text("📭 Немає даних.")
        return

    text = f"👥 **ЗВІТ ПО ПРАЦІВНИКАХ**\n{MONTH_NAMES[current_date.month]} {current_date.year}\n\n"

    for emp in employee_names_for_report(settings, transactions):
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


def calculate_tax_group(total_income, tax_config, year=CURRENT_TAX_RULES_YEAR):
    """Return standard monthly FOP taxes for the selected report year.

    VAT itself is never inferred from cash-flow transactions. For the
    ``3_percent_vat`` scheme the result therefore exposes metadata saying VAT
    is registered but not included in ``total_tax``.
    """
    profile = tax_profile_for_year(tax_config, year)
    group = profile['group']
    scheme = profile['scheme']
    rate = Decimal(str(profile['single_tax_rate']))

    def money(value, fallback='0'):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal(fallback)
        if not amount.is_finite():
            amount = Decimal(fallback)
        return amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    income = max(money(total_income), Decimal('0.00'))
    esv = money(profile['esv_fixed'])
    military = money(profile['military_fixed'])

    if group == 'none':
        single_tax = esv = military = Decimal('0.00')
        label = 'Не ФОП (фізособа)'
    elif group == 'fop1':
        single_tax = money(profile['fop1_fixed'])
        label = 'ФОП 1 група'
    elif group == 'fop2':
        single_tax = money(profile['fop2_fixed'])
        label = 'ФОП 2 група'
    else:
        single_tax = (income * rate).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        military = (
            income * Decimal(str(profile['military_rate']))
        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        label = 'ФОП 3 група'

    total_tax = single_tax + esv + military
    vat_registered = group == 'fop3' and scheme == '3_percent_vat'
    return {
        'group': group,
        'group_label': label,
        'scheme': scheme,
        'scheme_label': '3% + ПДВ' if vat_registered else '5% без ПДВ',
        'single_tax_rate': float(rate),
        'single_tax': float(single_tax),
        'esv': float(esv),
        'military_levy': float(military),
        'total_tax': float(total_tax),
        'vat_registered': vat_registered,
        'vat_included': False,
        'rules_year': int(year),
        'disclaimer': TAX_DISCLAIMER,
    }


async def show_tax_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show tax report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    settings = await user_settings_for(user_id)

    current_date = datetime.now(KYIV_TZ)
    transactions = await db.get_transactions(user_id, current_date.year, current_date.month)

    total_income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense')
    profit = total_income - total_expense

    tax = calculate_tax_group(
        total_income, settings['tax_config'], year=current_date.year)
    single_tax_label = (
        f"Єдиний ({tax['scheme_label']})"
        if tax['group'] == 'fop3' else 'Єдиний (фіксований)'
    )

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
    text += f"━━━ ПОДАТКИ · {tax['group_label']} ━━━\n"
    text += f"{single_tax_label}: {tax['single_tax']:.2f} грн\n"
    text += f"ЄСВ: {tax['esv']:.2f} грн\n"
    text += f"Військовий збір: {tax['military_levy']:.2f} грн\n"
    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"До сплати: {tax['total_tax']:.2f} грн\n\n"
    text += f"💰 Після податків: {profit - tax['total_tax']:.2f} грн\n\n"
    if tax['vat_registered']:
        text += "ПДВ у суму «До сплати» не включено.\n"
    text += f"ℹ️ {tax['disclaimer']}"

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

    entries = _simplified_accounting_entries(transactions)
    text = f"📚 **СПРОЩЕНИЙ ОБЛІК РУХУ КОШТІВ**\n{MONTH_NAMES[current_date.month]} {current_date.year}\n\n"
    text += f"━━━ БАЛАНС ━━━\n"
    text += f"Каса: {closing_balance:.2f} грн\n"
    text += f"Капітал: {opening_balance:.2f} грн\n"
    text += f"Прибуток: {profit:.2f} грн\n\n"
    text += f"━━━ ПРОВОДКИ ━━━\n"
    for entry in entries:
        text += (
            f"Дт {entry['debit']} - Кт {entry['credit']}: "
            f"{entry['amount']:.2f} грн · {entry['source_label']}\n"
        )
    text += "\n"
    text += f"━━━ РЕЗУЛЬТАТ ━━━\n"
    result_status = "прибуток ✅" if profit > 0 else "збиток ❌"
    text += f"{abs(profit):.2f} грн ({result_status})\n\n"
    text += f"ℹ️ {ACCOUNTING_DISCLAIMER}"

    await query.edit_message_text(text, parse_mode='Markdown')


async def show_ai_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate AI report"""
    query = update.callback_query
    user_id = str(update.effective_user.id)
    settings = await user_settings_for(user_id)

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
    for emp in settings['employees']:
        income = income_by_cat.get(f'Від {emp}', 0)
        salary = expense_by_cat.get(f'ЗП {emp}', 0)

        if income > 0 or salary > 0:
            roi = ((income - salary) / salary * 100) if salary > 0 else None
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
            roi_label = (
                '—' if emp_data['roi'] is None else f"{emp_data['roi']:.1f}%"
            )
            report += f"""{emp_data['name']}:
  Дохід: {emp_data['income']:.2f} UAH
  ЗП: {emp_data['salary']:.2f} UAH
  Прибуток: {emp_data['profit']:.2f} UAH
  ROI: {roi_label}

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


async def admin_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List every registered user — admin only."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна лише адміністратору.")
        return
    async with db_lock:
        cursor = db.conn.cursor()
        cursor.execute(
            "SELECT user_id, username, first_name, last_name, first_seen, last_seen "
            "FROM users ORDER BY first_seen"
        )
        rows = cursor.fetchall()
    if not rows:
        await update.message.reply_text("📭 У БД немає користувачів.")
        return
    lines = ["👥 *Users in DB*", ""]
    for r in rows:
        uid = r['user_id']
        name = (r['first_name'] or '') + (' ' + r['last_name'] if r['last_name'] else '')
        uname = f"@{r['username']}" if r['username'] else '—'
        seen = r['first_seen'][:10] if r['first_seen'] else '?'
        lines.append(f"`{uid}` · {name.strip() or '?'} · {uname} · {seen}")
    lines.append("")
    lines.append("Видалити фейкових: /cleanup\\_users")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')


async def admin_cleanup_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove only synthetic ``999000*`` QA accounts and all dependencies."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна лише адміністратору.")
        return
    admin_id = str(update.effective_user.id)
    decision = _admin_limiter.check((admin_id, 'cleanup_users'))
    if not decision.allowed:
        await update.message.reply_text(
            f'Зачекайте {decision.retry_after} с перед повторною операцією.'
        )
        return
    async with db_lock:
        candidates = db.conn.execute('''
            SELECT user_id, first_name, username
            FROM users
            WHERE user_id LIKE '999000%'
            ORDER BY user_id
        ''').fetchall()
    candidates = [row for row in candidates if str(row['user_id']) not in ADMIN_IDS]
    if not candidates:
        await update.message.reply_text(
            "✨ Нічого видаляти — синтетичних QA-акаунтів немає."
        )
        return
    removed = []
    for row in candidates:
        uid = str(row['user_id'])
        await db.delete_user_account(uid)
        removed.append((uid, row['first_name'] or '?', row['username']))
    await db.log_admin_action(
        admin_id,
        'cleanup_users',
        target='synthetic_users',
        status='ok',
        metadata={
            'removed_count': len(removed),
            'removed_ids_sha256': hashlib.sha256(
                ','.join(sorted(str(item[0]) for item in removed)).encode('utf-8')
            ).hexdigest(),
        },
    )
    text = f"🧹 *Cleanup done* · removed {len(removed)} users\n\n"
    for uid, name, uname in removed[:20]:
        text += f"`{uid}` · {name} · {('@' + uname) if uname else '—'}\n"
    await update.message.reply_text(text, parse_mode='Markdown')


async def admin_reset_user_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/reset_user_settings <user_id>` — wipe one user's user_settings row so
    they're reseeded from neutral DEFAULT_SETTINGS on next request.

    Special args:
      • `me` — reset the admin's own settings
      • `all` — reset every non-admin user (preserves admins)
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Команда доступна лише адміністратору.")
        return
    parts = (update.message.text or '').split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ''
    if not arg:
        await update.message.reply_text(
            "Використання:\n"
            "  `/reset_user_settings me` — скинути свої налаштування\n"
            "  `/reset_user_settings <user_id>` — скинути конкретного юзера\n"
            "  `/reset_user_settings all` — скинути всіх (крім адмінів)",
            parse_mode='Markdown',
        )
        return

    admin_id = str(update.effective_user.id)
    decision = _admin_limiter.check((admin_id, 'reset_user_settings'))
    if not decision.allowed:
        await update.message.reply_text(
            f'Зачекайте {decision.retry_after} с перед повторною операцією.'
        )
        return

    targets: list[str] = []
    if arg == 'me':
        targets = [str(update.effective_user.id)]
    elif arg == 'all':
        async with db_lock:
            cursor = db.conn.cursor()
            cursor.execute("SELECT user_id FROM user_settings")
            rows = cursor.fetchall()
        targets = [r['user_id'] for r in rows if r['user_id'] not in ADMIN_IDS]
        # ALSO reset admins if they explicitly say 'all-including-me' — but here we preserve them
    else:
        targets = [arg]

    if not targets:
        await update.message.reply_text("📭 Нікого скидати.")
        return

    reset_results = []
    for uid in targets:
        reset_results.append(await reset_user_configuration(uid))

    await db.log_admin_action(
        admin_id,
        'reset_user_settings',
        target=arg,
        status='ok',
        metadata={
            'target_count': len(targets),
            'budgets_deleted': sum(item['budgets'] for item in reset_results),
            'recurring_paused': sum(
                item['recurring_operations_paused'] for item in reset_results
            ),
        },
    )

    await update.message.reply_text(
        f"🧹 Скинуто налаштувань: {len(targets)}.\n"
        f"Наступне відкриття Mini App перезапише їх з нейтрального шаблону "
        f"(працівники = пусто, категорії = базові, ФОП 3 група).\n"
        f"Бюджети видалено, регулярні операції призупинено.\n"
        f"Транзакції та час НЕ зачеплено."
    )


async def _send_broadcast_with_bot(
    admin_id: str,
    message_text: str,
    telegram_bot,
    *,
    recipient_ids=None,
) -> dict:
    """Execute a previously confirmed in-chat broadcast."""
    user_ids = (
        list(recipient_ids)
        if recipient_ids is not None
        else await db.get_all_user_ids()
    )
    now_str = datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S')
    broadcast_id = await db.create_broadcast(message_text, now_str)
    sent = failed = skipped = 0
    receipts = []
    for uid in user_ids:
        if str(uid).startswith('999000'):
            skipped += 1
            receipts.append((uid, 'skipped', None, 'synthetic test id'))
            continue
        try:
            sent_message = await telegram_bot.send_message(chat_id=int(uid), text=message_text)
            sent += 1
            receipts.append((uid, 'sent', getattr(sent_message, 'message_id', None), None))
            await asyncio.sleep(0.05)
        except Exception as exc:
            failed += 1
            receipts.append((uid, 'failed', None, type(exc).__name__))
            logger.warning('broadcast failed for %s: %s', uid, exc)
    await db.save_broadcast_receipts(
        broadcast_id,
        receipts,
        now_str,
        sent=sent,
        failed=failed,
        skipped=skipped,
        total=len(user_ids),
    )
    await db.log_admin_action(
        admin_id,
        'broadcast',
        target='all_users',
        metadata={
            'broadcast_id': broadcast_id,
            'text_sha256': _broadcast_text_digest(message_text),
            'text_length': len(message_text),
            'sent': sent,
            'failed': failed,
            'skipped': skipped,
        },
    )
    return {
        'broadcast_id': broadcast_id,
        'sent': sent,
        'failed': failed,
        'skipped': skipped,
        'total_users': len(user_ids),
    }


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Preview a broadcast; sending requires the inline confirmation."""
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
    admin_id = str(update.effective_user.id)
    message_text = parts[1].strip()
    if len(message_text) > 4096:
        await update.message.reply_text('Текст розсилки не може перевищувати 4096 символів.')
        return
    user_ids = await db.get_all_user_ids()
    if not user_ids:
        await update.message.reply_text("📭 Поки що немає користувачів у БД.")
        return
    skipped = sum(str(uid).startswith('999000') for uid in user_ids)
    token, _expires_at = _issue_broadcast_confirmation(
        admin_id,
        message_text,
        recipient_ids=user_ids,
    )
    controls = (
        f'Отримувачів: {len(user_ids) - skipped}; пропущено QA: {skipped}.\n'
        'Надсилання почнеться лише після підтвердження.'
    )
    markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                'Надіслати всім',
                callback_data=f'admin_broadcast:confirm:{token}',
            ),
            InlineKeyboardButton(
                'Скасувати',
                callback_data=f'admin_broadcast:cancel:{token}',
            ),
        ]])
    if len(message_text) > 3500:
        await update.message.reply_text('📣 Попередній перегляд розсилки:')
        await update.message.reply_text(message_text)
        await update.message.reply_text(controls, reply_markup=markup)
    else:
        await update.message.reply_text(
            f'📣 Попередній перегляд розсилки\n\n{message_text}\n\n{controls}',
            reply_markup=markup,
        )


async def daily_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Create a consistent snapshot and verify/prune an off-site copy."""
    admin_id = sorted(ADMIN_IDS)[0] if ADMIN_IDS else None
    backup_dir = Path(DATA_DIR) / 'backups'
    try:
        if not os.path.exists(DB_FILE):
            return
        artifact = await asyncio.to_thread(
            create_sqlite_snapshot,
            DB_FILE,
            backup_dir,
            prefix='finance',
        )
        config = S3BackupConfig.from_env()
        if config is not None:
            remote = await asyncio.to_thread(upload_and_verify_snapshot, artifact, config)
            backup_status['last_remote_key'] = remote.key
            logger.info('verified off-site backup s3://%s/%s', remote.bucket, remote.key)
            retention_days = _positive_env_int('BACKUP_REMOTE_RETENTION_DAYS', 30)
            await asyncio.to_thread(
                prune_remote_backups,
                config,
                retention_days=retention_days,
            )
        elif _env_flag('BACKUP_REQUIRED'):
            raise BackupError('off-site backup is required but not configured')
        else:
            logger.warning('off-site backup is not configured; retained verified local snapshot only')

        backup_status.update({
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_error': None,
            'last_checksum': artifact.sha256,
        })
    except Exception as e:
        backup_status['last_error'] = type(e).__name__
        logger.warning(f"backup failed: {e}")
        if admin_id is not None:
            try:
                await context.bot.send_message(
                    chat_id=int(admin_id),
                    text=f'⚠️ Ruby Finance backup failed: {type(e).__name__}',
                )
            except Exception:
                pass
    finally:
        # A remote outage must not fill the Railway Volume with local copies.
        retain = _positive_env_int('BACKUP_LOCAL_RETENTION', 7)
        try:
            snapshots = sorted(
                backup_dir.glob('finance-*.db'),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for stale in snapshots[retain:]:
                stale.unlink(missing_ok=True)
        except Exception:
            logger.exception('local backup retention cleanup failed')


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
async def json_errors_middleware(request: web.Request, handler):
    """Convert framework 4xx/5xx (and uncaught exceptions) to JSON
    `{"detail": "..."}` so the Mini App's error handler never sees plain text
    or HTML. This catches things like 405 Method Not Allowed, 404 from the
    router, and any unhandled exception inside a handler (e.g. NaN crash)."""
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        # aiohttp uses HTTPException for 4xx/5xx routing/method errors
        if request.path.startswith('/api'):
            return _json_response({'detail': e.reason or 'Error'}, status=e.status)
        raise
    except Exception as e:  # pragma: no cover — catch-all safety net
        if request.path.startswith('/api'):
            logger.exception(f'unhandled in {request.method} {request.path}: {e}')
            return _json_response({'detail': 'internal error'}, status=500)
        raise
    # Convert non-JSON 4xx/5xx responses (e.g. aiohttp's default 405 text) to JSON
    if (request.path.startswith('/api')
            and resp.status >= 400
            and resp.content_type != 'application/json'):
        return _json_response({'detail': resp.reason or 'Error'}, status=resp.status)
    return resp

# Origins that legitimately host our Mini App.
# Telegram WebView (iOS/Android native) does not send Origin (or sends 'null'),
# so we let those through too — the initData HMAC remains the real auth gate.
def _origin(value):
    try:
        parsed = urlsplit(str(value or '').strip())
    except ValueError:
        return None
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None
    return f'{parsed.scheme}://{parsed.netloc}'


def _cors_allowed_origins():
    origins = {
        'https://web.telegram.org',
        'https://web.telegram.com',
        'https://t.me',
    }
    public_origin = _origin(os.getenv(
        'MINIAPP_PUBLIC_URL',
        'https://finance-bot-production-5de8.up.railway.app',
    ))
    if public_origin:
        origins.add(public_origin)
    for raw in os.getenv('CORS_ALLOWED_ORIGINS', '').split(','):
        allowed = _origin(raw)
        if allowed:
            origins.add(allowed)
    return origins


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Add CORS headers, handle OPTIONS preflight. Reflects allow-listed
    origins so cross-site pages can't read authenticated responses; missing
    Origin (Telegram iOS/Android WebView) is allowed because there's no
    cross-origin attack surface in that case."""
    origin = request.headers.get('Origin', '')

    if request.method == 'OPTIONS':
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    if not origin or origin == 'null':
        # Native Telegram WebView — no browser CORS context, safe to allow.
        allow = '*'
    elif origin in _cors_allowed_origins():
        allow = origin
    else:
        allow = None
    if allow is not None:
        resp.headers['Access-Control-Allow-Origin'] = allow
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Telegram-Init-Data'
    resp.headers['Access-Control-Allow-Methods'] = (
        'GET, POST, PUT, PATCH, DELETE, OPTIONS'
    )
    resp.headers['Vary'] = 'Origin'
    return resp


@web.middleware
async def init_data_middleware(request: web.Request, handler):
    """Validate Telegram initData; attach user_id and tg_user to request."""
    if request.method == 'OPTIONS':
        return await handler(request)

    if request.path in _SKIP_AUTH_PATHS:
        return await handler(request)

    raw = request.headers.get('X-Telegram-Init-Data', '')

    bot_token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    parsed, validation_code = (
        validate_init_data_result(raw, bot_token)
        if raw else (None, 'INVALID_INIT_DATA')
    )

    if parsed is None:
        code = validation_code or 'INVALID_INIT_DATA'
        detail = (
            'Telegram session expired. Reopen the Mini App.'
            if code == 'INIT_DATA_EXPIRED'
            else 'Invalid initData'
        )
        return web.json_response({'detail': detail, 'code': code}, status=401)

    tg_user = parsed.get('user') or {}
    user_id_val = tg_user.get('id')
    # Reject auth that passes HMAC but carries no user.id — without this guard
    # every such request would aggregate into one shared "" bucket in the DB.
    if not user_id_val:
        return web.json_response(
            {'detail': 'user id missing', 'code': 'INVALID_INIT_DATA'}, status=401)
    request['user_id'] = str(user_id_val)
    request['tg_user'] = tg_user
    return await handler(request)


def _rate_limited_response(decision):
    response = _json_response(
        {'detail': 'Too many requests. Please retry later.', 'code': 'RATE_LIMITED'},
        status=429,
    )
    response.headers.update(decision.headers())
    return response


@web.middleware
async def preauth_rate_limit_middleware(request: web.Request, handler):
    """Best-effort protection for public endpoints and invalid-HMAC floods."""
    if request.method == 'OPTIONS':
        return await handler(request)
    key = request.remote or 'unknown'
    decision = _preauth_limiter.check((key, request.path))
    if not decision.allowed:
        return _rate_limited_response(decision)
    return await handler(request)


@web.middleware
async def user_rate_limit_middleware(request: web.Request, handler):
    """Per-user quotas after successful Telegram authentication."""
    if request.method == 'OPTIONS' or request.path in _SKIP_AUTH_PATHS:
        return await handler(request)
    user_id = str(request.get('user_id', ''))
    if not user_id:
        return _json_response({'detail': 'Invalid initData'}, status=401)

    if request.path.startswith('/api/admin/'):
        limiter = _admin_limiter
    elif request.method in {'POST', 'PATCH', 'PUT', 'DELETE'}:
        limiter = _write_limiter
    else:
        limiter = _read_limiter
    decision = limiter.check(user_id)
    if not decision.allowed:
        return _rate_limited_response(decision)
    response = await handler(request)
    response.headers.update(decision.headers())
    return response


@web.middleware
async def user_context_middleware(request: web.Request, handler):
    """Register/auth-context only after the request passes its user quota."""
    if request.method == 'OPTIONS' or request.path in _SKIP_AUTH_PATHS:
        return await handler(request)
    owner = str(request['user_id'])
    async with _account_request_locks[owner]:
        tg_user = request.get('tg_user') or {}
        try:
            await db.upsert_user(_UserObj(tg_user))
        except Exception as exc:
            logger.warning(
                'upsert_user via middleware failed: %s', type(exc).__name__
            )
        settings_write = (
            request.method in {'POST', 'PATCH', 'DELETE'}
            and request.path.startswith(('/api/categories', '/api/employees',
                                         '/api/time-categories', '/api/settings'))
        )
        if settings_write:
            async with _user_settings_locks[owner]:
                async with _recurring_user_locks[owner]:
                    request['_settings_lock_held'] = True
                    try:
                        return await handler(request)
                    finally:
                        request.pop('_settings_lock_held', None)
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
    return all(
        isinstance(exchange_rates_cache.get(currency), (int, float))
        and math.isfinite(exchange_rates_cache[currency])
        and exchange_rates_cache[currency] > 0
        for currency in ('USD', 'EUR')
    )


def _exchange_rate_unavailable_response():
    return _json_response({
        'detail': 'Exchange rates are temporarily unavailable. Please retry later.',
        'code': 'EXCHANGE_RATE_UNAVAILABLE',
    }, status=503)


# ---- route handlers ----

async def api_health(request: web.Request):
    try:
        async with db_lock:
            db.conn.execute('SELECT 1').fetchone()
        database_ok = True
    except Exception:
        logger.exception('health database probe failed')
        database_ok = False
    try:
        offsite_configured = S3BackupConfig.from_env() is not None
        backup_config_error = None
    except BackupError as exc:
        offsite_configured = False
        backup_config_error = type(exc).__name__
    backup_required = _env_flag('BACKUP_REQUIRED')
    backup_ready = bool(
        offsite_configured
        and backup_status.get('last_success')
        and backup_status.get('last_remote_key')
        and not backup_status.get('last_error')
    )
    service_ready = database_ok and (not backup_required or backup_ready)
    payload = {
        'ok': service_ready,
        'service': 'ruby-finance-api',
        'build': os.getenv('RAILWAY_GIT_COMMIT_SHA') or os.getenv('BUILD_ID'),
        'database': 'ok' if database_ok else 'error',
        'backup': {
            'required': backup_required,
            'ready': backup_ready,
            'offsite_configured': offsite_configured,
            'configuration_error': backup_config_error,
            'last_success': backup_status['last_success'],
            'last_error': backup_status['last_error'],
        },
    }
    return _json_response(payload, status=200 if service_ready else 503)


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
    if not await _ensure_fresh_rates():
        return _exchange_rate_unavailable_response()
    last = exchange_rates_cache.get('last_update')
    return _json_response({
        'USD': exchange_rates_cache.get('USD'),
        'EUR': exchange_rates_cache.get('EUR'),
        'updated_at': last.isoformat() if last else None,
    })


async def api_balance(request: web.Request):
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err
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
    """List user's transactions with optional filters:

      ?limit=N                     (default 15, max 100; if 'all' →
                                    hard_cap 100 ignored, cap raised to 5 000)
      ?type=income|expense
      ?period=current_month|10d|30d|month   (convenience presets)
      ?year=YYYY&month=MM          (when period=month, OR direct override)
      ?from=YYYY-MM-DD&to=YYYY-MM-DD  (explicit date range — takes precedence)

    Backwards-compatible: no filters → latest 15 across all types.
    """
    user_id = request['user_id']
    q = request.rel_url.query

    # Optional type filter
    t_type = q.get('type')
    if t_type and t_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)

    # Period → date range translation
    from_date = q.get('from')
    to_date = q.get('to')
    period = q.get('period')
    now = datetime.now(KYIV_TZ)

    if not from_date and not to_date and period:
        if period == 'current_month':
            year_v, month_v = now.year, now.month
            import calendar
            last_day = calendar.monthrange(year_v, month_v)[1]
            from_date = f'{year_v:04d}-{month_v:02d}-01'
            to_date   = f'{year_v:04d}-{month_v:02d}-{last_day:02d}'
        elif period == '10d':
            from datetime import timedelta
            from_date = (now - timedelta(days=9)).strftime('%Y-%m-%d')
            to_date   = now.strftime('%Y-%m-%d')
        elif period == '30d':
            from datetime import timedelta
            from_date = (now - timedelta(days=29)).strftime('%Y-%m-%d')
            to_date   = now.strftime('%Y-%m-%d')
        elif period == 'month':
            # specific month — use ?year & ?month
            year_v, month_v, err = _parse_year_month(request)
            if err is not None:
                return err
            import calendar
            last_day = calendar.monthrange(year_v, month_v)[1]
            from_date = f'{year_v:04d}-{month_v:02d}-01'
            to_date   = f'{year_v:04d}-{month_v:02d}-{last_day:02d}'
        else:
            return _json_response(
                {'detail': 'period must be one of: current_month, 10d, 30d, month'},
                status=400,
            )

    # Validate explicit from/to (loose — SQLite text comparison works for ISO dates)
    for k, v in (('from', from_date), ('to', to_date)):
        if v and not _looks_like_iso_date(v):
            return _json_response({'detail': f'{k} must be YYYY-MM-DD'}, status=400)
    if from_date and to_date and from_date > to_date:
        return _json_response({'detail': 'from must not be after to'}, status=400)

    # Limit. 'all' raises the cap so history filters can return everything in range.
    limit_raw = q.get('limit')
    if limit_raw == 'all':
        limit = 5000
    else:
        limit, err = _parse_limit(request, default=15, hard_cap=5000)
        if err is not None:
            return err

    rows = await db.get_transactions(
        user_id,
        limit=limit,
        t_type=t_type,
        from_date=from_date,
        to_date=to_date,
    )
    result = [
        {
            'id': r['id'],
            'amount': r['amount'],
            'currency': r['currency'],
            'amount_uah': r['amount_uah'],
            'type': r['type'],
            'category': r['category'],
            'subcategory': (r['subcategory'] if 'subcategory' in r.keys() else None),
            'payment_source': (
                r['payment_source'] if 'payment_source' in r.keys() else None
            ),
            'description': r['description'],
            'date': r['date'],
            'timestamp': r['timestamp'],
        }
        for r in rows
    ]
    return _json_response(result)


def _quick_operation_payload(row: dict):
    """Expose only date-independent fields that are safe to repeat."""
    return {
        'amount': float(row['amount']),
        'currency': row['currency'],
        'type': row['type'],
        'category': row['category'],
        'subcategory': row.get('subcategory') or None,
        'payment_source': row.get('payment_source'),
        'comment': row.get('description') or '',
    }


def _quick_operation_is_current(row: dict, settings: dict) -> bool:
    category = (
        settings.get('categories', {})
        .get(row.get('type'), {})
        .get(row.get('category'))
    )
    if not isinstance(category, dict):
        return False
    subcategory = row.get('subcategory') or None
    return not subcategory or subcategory in (category.get('subcategories') or [])


async def api_quick_templates(request: web.Request):
    """Return frequent operations and the latest operation for quick-add UI."""
    raw_limit = request.rel_url.query.get('limit')
    if raw_limit is None:
        limit = 5
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return _json_response(
                {'detail': 'limit must be an integer between 1 and 10'},
                status=400,
            )
        if not 1 <= limit <= 10:
            return _json_response(
                {'detail': 'limit must be between 1 and 10'}, status=400
            )

    # Fetch extra candidates because stale historical categories are filtered
    # below and must not crowd out usable templates.
    templates, last_operation = await db.get_quick_templates(
        request['user_id'], limit=max(50, limit * 20)
    )
    settings = await user_settings_for(request['user_id'])
    result = []
    for row in templates:
        if not _quick_operation_is_current(row, settings):
            continue
        item = _quick_operation_payload(row)
        item['usage_count'] = int(row['usage_count'])
        result.append(item)
        if len(result) >= limit:
            break

    return _json_response({
        'templates': result,
        'last_operation': (
            _quick_operation_payload(last_operation)
            if last_operation and _quick_operation_is_current(last_operation, settings)
            else None
        ),
    })


def _looks_like_iso_date(s: str) -> bool:
    try:
        datetime.strptime(s, '%Y-%m-%d')
        return True
    except (TypeError, ValueError):
        return False


_CLIENT_REQUEST_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


def _parse_client_request_id(body: dict):
    """Validate an optional bounded, log-safe idempotency token."""
    if 'client_request_id' not in body or body['client_request_id'] is None:
        return None, None
    value = body['client_request_id']
    if not isinstance(value, str) or not _CLIENT_REQUEST_ID_PATTERN.fullmatch(value):
        return None, _json_response({
            'detail': (
                'client_request_id must be a 1-128 character ASCII token '
                'using letters, digits, dot, underscore, colon, or hyphen'
            )
        }, status=400)
    return value, None


def _parse_payment_source(body: dict, *, required=False):
    if 'payment_source' not in body:
        if required:
            return None, _json_response(
                {'detail': 'payment_source is required'}, status=400
            )
        return None, None
    value = body['payment_source']
    if value is None:
        return None, None
    if not isinstance(value, str) or value not in PAYMENT_SOURCES:
        return None, _json_response({
            'detail': 'payment_source must be cash, card, transfer, other, or null'
        }, status=400)
    return value, None


def _transaction_write_response(row: dict, *, duplicate: bool):
    """Stable POST response for both a new write and an idempotent replay."""
    client_request_id = row.get('client_request_id')
    return {
        'id': row['id'],
        'amount': row['amount'],
        'currency': row['currency'],
        'amount_uah': row['amount_uah'],
        'type': row['type'],
        'category': row['category'],
        'subcategory': row.get('subcategory'),
        'payment_source': row.get('payment_source'),
        'description': row.get('description'),
        'date': row['date'],
        'timestamp': row['timestamp'],
        'client_request_id': client_request_id,
        'duplicate': duplicate,
        'idempotent': client_request_id is not None,
    }


def _idempotency_payload_matches(
    row: dict,
    *,
    request_fingerprint: str,
    amount: float,
    currency: str,
    t_type: str,
    category: str,
    subcategory: str | None,
    payment_source: str | None,
    description: str,
) -> bool:
    stored_fingerprint = row.get('request_fingerprint')
    if stored_fingerprint:
        return hmac.compare_digest(stored_fingerprint, request_fingerprint)
    # Compatibility for rows written before request fingerprints existed.
    return (
        math.isclose(float(row['amount']), amount, rel_tol=0, abs_tol=1e-9)
        and row['currency'] == currency
        and row['type'] == t_type
        and row['category'] == category
        and (row.get('subcategory') or None) == subcategory
        and row.get('payment_source') == payment_source
        and (row.get('description') or '') == description
    )


def _idempotency_conflict_response():
    return _json_response({
        'detail': 'client_request_id was already used for a different transaction',
        'code': 'IDEMPOTENCY_CONFLICT',
    }, status=409)


async def api_post_transaction(request: web.Request):
    user_id = request['user_id']
    tg_user = request['tg_user']

    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    if not isinstance(body, dict):
        return _json_response({'detail': 'JSON body must be an object'}, status=400)

    client_request_id, err = _parse_client_request_id(body)
    if err is not None:
        return err
    payment_source, err = _parse_payment_source(body)
    if err is not None:
        return err
    t_type = body.get('type')
    if t_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)

    raw_amount = body.get('amount')
    if raw_amount is None or isinstance(raw_amount, bool):
        return _json_response({'detail': 'amount required and must be a number'}, status=400)
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        return _json_response({'detail': 'amount must be a number'}, status=400)
    # Reject NaN / Inf (Python's json parser accepts these, breaks downstream)
    import math
    if not math.isfinite(amount):
        return _json_response({'detail': 'amount must be a finite number'}, status=400)
    # Positive only — negative/zero amounts silently corrupt every report.
    if amount <= 0:
        return _json_response({'detail': 'amount must be > 0'}, status=400)
    # Cap to a sane upper bound (1 billion of any currency)
    if amount > 1_000_000_000:
        return _json_response({'detail': 'amount too large'}, status=400)

    currency = str(body.get('currency', 'UAH')).upper()
    if currency not in ('UAH', 'USD', 'EUR'):
        return _json_response({'detail': 'currency must be UAH, USD, or EUR'}, status=400)

    category = _clean_text(body.get('category'), max_len=80, default='Інше')
    description = _clean_text(body.get('description'), max_len=200, default='')
    # Optional subcategory (hierarchical categories). None when absent/empty.
    subcategory = _clean_text(body.get('subcategory'), max_len=80, default='') or None
    request_fingerprint = _transaction_request_fingerprint(
        amount=amount,
        currency=currency,
        t_type=t_type,
        category=category,
        subcategory=subcategory,
        payment_source=payment_source,
        description=description,
    )

    if client_request_id is not None:
        existing = await db.get_transaction_by_client_request_id(
            user_id, client_request_id
        )
        if existing is not None:
            if not _idempotency_payload_matches(
                existing,
                request_fingerprint=request_fingerprint,
                amount=amount,
                currency=currency,
                t_type=t_type,
                category=category,
                subcategory=subcategory,
                payment_source=payment_source,
                description=description,
            ):
                return _idempotency_conflict_response()
            await db.upsert_user(_UserObj(tg_user))
            logger.info(
                "API POST /api/transactions idempotent replay "
                f"user={user_id} id={existing['id']}"
            )
            return _json_response(
                _transaction_write_response(existing, duplicate=True)
            )

    user_settings = await user_settings_for(user_id)
    category_entry = user_settings.get('categories', {}).get(t_type, {}).get(category)
    if not isinstance(category_entry, dict):
        return _json_response({'detail': f'unknown {t_type} category "{category}"'}, status=400)
    known_subcategories = category_entry.get('subcategories') or []
    if subcategory and subcategory not in known_subcategories:
        return _json_response(
            {'detail': f'unknown subcategory "{subcategory}" for category "{category}"'},
            status=400,
        )

    try:
        rate = await get_exchange_rate(currency)
    except ExchangeRateUnavailableError:
        return _exchange_rate_unavailable_response()
    amount_uah = round(convert_to_uah(amount, currency, rate), 2)
    # Reject sub-kopiyka amounts: storing amount=0.001 UAH and amount_uah=0.0
    # would silently delete the entry from reports (display says 0.001, math
    # uses 0). Force the user to pick a meaningful figure.
    if amount_uah < 0.01:
        return _json_response(
            {'detail': 'amount too small (UAH equivalent must be at least 0.01)'}, status=400)

    now = datetime.now(KYIV_TZ)
    date_str = now.strftime('%Y-%m-%d')
    ts_str = now.strftime('%Y-%m-%d %H:%M:%S')

    try:
        row_id = await db.add_transaction(
            user_id, amount, currency, amount_uah,
            t_type, category, description, date_str, ts_str,
            subcategory=subcategory,
            client_request_id=client_request_id,
            payment_source=payment_source,
            request_fingerprint=request_fingerprint,
        )
    except sqlite3.IntegrityError:
        # Two concurrent retries can both miss the preflight lookup. The
        # user-scoped unique index is the final authority; return the winner.
        if client_request_id is None:
            raise
        existing = await db.get_transaction_by_client_request_id(
            user_id, client_request_id
        )
        if existing is None:
            raise
        if not _idempotency_payload_matches(
            existing,
            request_fingerprint=request_fingerprint,
            amount=amount,
            currency=currency,
            t_type=t_type,
            category=category,
            subcategory=subcategory,
            payment_source=payment_source,
            description=description,
        ):
            return _idempotency_conflict_response()
        await db.upsert_user(_UserObj(tg_user))
        logger.info(
            "API POST /api/transactions concurrent idempotent replay "
            f"user={user_id} id={existing['id']}"
        )
        return _json_response(
            _transaction_write_response(existing, duplicate=True)
        )

    await db.upsert_user(_UserObj(tg_user))

    logger.info(
        "API POST /api/transactions user=%s id=%s type=%s",
        user_id,
        row_id,
        t_type,
    )
    created = {
        'id': row_id,
        'amount': amount,
        'currency': currency,
        'amount_uah': amount_uah,
        'type': t_type,
        'category': category,
        'subcategory': subcategory,
        'payment_source': payment_source,
        'description': description,
        'date': date_str,
        'timestamp': ts_str,
        'client_request_id': client_request_id,
    }
    return _json_response(
        _transaction_write_response(created, duplicate=False), status=201
    )


async def api_patch_transaction(request: web.Request):
    """Correct only the authenticated owner's payment source classification."""
    try:
        transaction_id = int(request.match_info['id'])
    except (KeyError, TypeError, ValueError):
        return _json_response({'detail': 'Invalid id'}, status=400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    if not isinstance(body, dict):
        return _json_response({'detail': 'JSON body must be an object'}, status=400)

    payment_source, err = _parse_payment_source(body, required=True)
    if err is not None:
        return err
    row = await db.update_transaction_payment_source(
        transaction_id, request['user_id'], payment_source
    )
    if row is None:
        return _json_response({'detail': 'Not found'}, status=404)
    return _json_response({
        'id': row['id'],
        'amount': row['amount'],
        'currency': row['currency'],
        'amount_uah': row['amount_uah'],
        'type': row['type'],
        'category': row['category'],
        'subcategory': row.get('subcategory'),
        'payment_source': row.get('payment_source'),
        'description': row.get('description'),
        'date': row['date'],
        'timestamp': row['timestamp'],
    })


async def api_delete_transaction(request: web.Request):
    user_id = request['user_id']
    try:
        tx_id = int(request.match_info['id'])
    except (KeyError, ValueError):
        return _json_response({'detail': 'Invalid id'}, status=400)

    # Scoped DELETE — rowcount tells us atomically whether anything matched.
    # No race window between Python-level check and SQL delete.
    deleted = await db.delete_transaction(tx_id, user_id=user_id)
    if not deleted:
        return _json_response({'detail': 'Not found'}, status=404)
    logger.info(f"API DELETE /api/transactions/{tx_id} user={user_id}")
    return web.Response(status=204)


def _round_money(value) -> float:
    return float(
        Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    )


def _payment_source_summary(rows):
    keys = (*PAYMENT_SOURCES, PAYMENT_SOURCE_UNCLASSIFIED)
    income = {key: Decimal('0') for key in keys}
    expense = {key: Decimal('0') for key in keys}
    for row in rows:
        raw_source = row.get('payment_source')
        source = (
            raw_source
            if raw_source in PAYMENT_SOURCES
            else PAYMENT_SOURCE_UNCLASSIFIED
        )
        target = income if row['type'] == 'income' else expense
        target[source] += Decimal(str(row['amount_uah']))
    return {
        'income_by_payment_source': {
            key: _round_money(value) for key, value in income.items()
        },
        'expense_by_payment_source': {
            key: _round_money(value) for key, value in expense.items()
        },
    }


ACCOUNTING_MODEL = 'simplified_cash_movement'
ACCOUNTING_DISCLAIMER = (
    'Спрощена класифікація руху коштів, а не повний бухгалтерський облік. '
    'Рахунки 301/311 показують джерело коштів; перевіряйте проводки з бухгалтером.'
)
_ACCOUNTING_SOURCE_META = {
    'cash': ('301', 'cash', 'Готівка'),
    'card': ('311', 'bank', 'Картка'),
    'transfer': ('311', 'bank', 'Переказ'),
    'other': ('—', 'other', 'Інше джерело'),
    PAYMENT_SOURCE_UNCLASSIFIED: (
        '—', 'unclassified', 'Не класифіковано'
    ),
}


def _simplified_accounting_entries(rows):
    totals = defaultdict(Decimal)
    for row in rows:
        source = row.get('payment_source')
        if source not in PAYMENT_SOURCES:
            source = PAYMENT_SOURCE_UNCLASSIFIED
        totals[(row['type'], source)] += Decimal(str(row['amount_uah']))

    entries = []
    for kind in ('income', 'expense'):
        for source in (*PAYMENT_SOURCES, PAYMENT_SOURCE_UNCLASSIFIED):
            amount = totals[(kind, source)]
            if amount == 0:
                continue
            account, source_class, source_label = _ACCOUNTING_SOURCE_META[source]
            entries.append({
                'type': kind,
                'payment_source': source,
                'source_class': source_class,
                'source_label': source_label,
                'debit': account if kind == 'income' else '901',
                'credit': '701' if kind == 'income' else account,
                'amount': _round_money(amount),
                'label': (
                    f'Надходження · {source_label}'
                    if kind == 'income'
                    else f'Видатки · {source_label}'
                ),
            })
    return entries


async def api_monthly_report(request: web.Request):
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err
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

    source_summary = _payment_source_summary(rows)
    return _json_response({
        'income_by_category': income_by_cat,
        'expense_by_category': expense_by_cat,
        **source_summary,
        'total_income': round(total_income, 2),
        'total_expense': round(total_expense, 2),
        'transaction_count': len(rows),
    })


async def api_report_payment_sources(request: web.Request):
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err
    rows = await db.get_transactions(user_id, year=year, month=month)
    summary = _payment_source_summary(rows)
    return _json_response({
        'year': year,
        'month': month,
        **summary,
        'total_income': _round_money(sum(
            Decimal(str(row['amount_uah']))
            for row in rows if row['type'] == 'income'
        )),
        'total_expense': _round_money(sum(
            Decimal(str(row['amount_uah']))
            for row in rows if row['type'] == 'expense'
        )),
        'transaction_count': len(rows),
    })


async def api_categories(request: web.Request):
    user_settings = await user_settings_for(request['user_id'])
    cats = user_settings.get('categories', {})
    expense_names = list(cats.get('expense', {}).keys())
    income_names = list(cats.get('income', {}).keys())
    return _json_response({'expense': expense_names, 'income': income_names})


async def api_settings(request: web.Request):
    s = await user_settings_for(request['user_id'])
    tax_config = s.get('tax_config', {})
    return _json_response({
        'employees': s.get('employees', []),
        'tax_config': tax_config,
        'tax_year': CURRENT_TAX_RULES_YEAR,
        'tax_profile': tax_profile_for_year(
            tax_config, CURRENT_TAX_RULES_YEAR),
        'tax_profiles': {
            str(year): tax_profile_for_year(tax_config, year)
            for year in sorted(TAX_RULES_BY_YEAR)
        },
        'supported_tax_years': sorted(TAX_RULES_BY_YEAR),
    })


@serialized_user_settings_write
async def api_settings_reset(request: web.Request):
    """Wipe this user's settings row → next request rebuilds it from
    DEFAULT_SETTINGS. Used by the «Reset to defaults» button in the
    Mini App, and as the recovery path for users who imported legacy
    employees/categories they didn't actually have. Budgets are deleted and
    every recurring template is paused; transaction/time history is retained."""
    user_id = request['user_id']
    await db.reset_user_settings(user_id)
    logger.info(f"API DELETE /api/settings user={user_id} (reset to defaults)")
    fresh = await user_settings_for(user_id)
    return _json_response(fresh)


# ---- helpers for parity endpoints ----

def _parse_year_month(request: web.Request):
    """Parse year/month query params, defaulting to current Kyiv month."""
    now = datetime.now(KYIV_TZ)
    try:
        year = int(request.rel_url.query.get('year', now.year))
        month = int(request.rel_url.query.get('month', now.month))
    except (TypeError, ValueError):
        return None, None, _json_response({'detail': 'Invalid year/month'}, status=400)
    if not (1 <= month <= 12):
        return None, None, _json_response({'detail': 'month must be 1-12'}, status=400)
    # Reject silly years (year=0 produced '0000-01-01' periods)
    if not (2000 <= year <= now.year + 1):
        return None, None, _json_response(
            {'detail': f'year must be between 2000 and {now.year + 1}'}, status=400)
    return year, month, None


def _parse_limit(request: web.Request, default: int = 15, hard_cap: int = 500):
    """Parse ?limit=N. Returns (limit, err_response_or_None).
    Rejects non-int, <=0; clamps to hard_cap."""
    raw = request.rel_url.query.get('limit')
    if raw is None:
        return default, None
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return None, _json_response({'detail': 'limit must be a positive integer'}, status=400)
    if val < 1:
        return None, _json_response({'detail': 'limit must be >= 1'}, status=400)
    return min(val, hard_cap), None


def _clean_text(value, max_len: int, default: str = '') -> str:
    """Coerce arbitrary JSON value to a safe text:
      • None / non-str → default
      • strip NULL bytes (break Telegram rendering, log tooling)
      • truncate to max_len
    """
    if value is None:
        return default
    s = value if isinstance(value, str) else str(value)
    s = s.replace('\x00', '').strip()
    if not s:
        return default
    return s[:max_len]


# ---- budgets (per-user, monthly UAH limits) ----

def _parse_budget_identity(body):
    if not isinstance(body, dict):
        return None, None, _json_response(
            {'detail': 'JSON body must be an object'}, status=400
        )
    budget_type = body.get('type')
    if budget_type not in ('income', 'expense'):
        return None, None, _json_response(
            {'detail': 'type must be income or expense'}, status=400
        )
    raw_category = body.get('category')
    if not isinstance(raw_category, str):
        return None, None, _json_response(
            {'detail': 'category must be a string'}, status=400
        )
    category = raw_category.strip()
    if not category or len(category) > 80:
        return None, None, _json_response(
            {'detail': 'category must be 1-80 characters'}, status=400
        )
    return budget_type, category, None


def _parse_budget_limit(body):
    raw = body.get('monthly_limit_uah') if isinstance(body, dict) else None
    if raw is None or isinstance(raw, bool):
        return None, _json_response(
            {'detail': 'monthly_limit_uah must be a positive number'}, status=400
        )
    try:
        value = Decimal(str(raw))
        if not value.is_finite() or value <= 0 or value > Decimal('1000000000000'):
            raise ValueError
        value = value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if value < Decimal('0.01'):
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        return None, _json_response({
            'detail': (
                'monthly_limit_uah must be between 0.01 and 1000000000000'
            )
        }, status=400)
    return value, None


@serialized_user_settings_write
async def api_budgets_put(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    budget_type, category, err = _parse_budget_identity(body)
    if err is not None:
        return err
    monthly_limit, err = _parse_budget_limit(body)
    if err is not None:
        return err

    settings = await user_settings_for(request['user_id'])
    category_entry = (
        settings.get('categories', {}).get(budget_type, {}).get(category)
    )
    if not isinstance(category_entry, dict):
        return _json_response({'detail': 'unknown category'}, status=400)

    row = await db.upsert_budget(
        request['user_id'], budget_type, category, float(monthly_limit)
    )
    return _json_response({
        'type': row['type'],
        'category': row['category'],
        'monthly_limit_uah': _round_money(row['monthly_limit_uah']),
    })


def _month_date_bounds(year, month):
    start = f'{year:04d}-{month:02d}-01'
    if month == 12:
        end = f'{year + 1:04d}-01-01'
    else:
        end = f'{year:04d}-{month + 1:02d}-01'
    return start, end


async def api_budgets_get(request: web.Request):
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err
    start, end = _month_date_bounds(year, month)
    rows = await db.get_budget_progress(request['user_id'], start, end)
    budgets = []
    for row in rows:
        limit_value = Decimal(str(row['monthly_limit_uah'])).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        spent = Decimal(str(row['spent_uah'])).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        remaining = limit_value - spent
        progress = (spent * Decimal('100') / limit_value).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP
        )
        budgets.append({
            'type': row['type'],
            'category': row['category'],
            'monthly_limit_uah': _round_money(limit_value),
            'spent_uah': _round_money(spent),
            'remaining_uah': _round_money(remaining),
            'progress_percent': float(progress),
            'is_exceeded': spent > limit_value,
        })
    return _json_response({
        'year': year,
        'month': month,
        'currency': 'UAH',
        'budgets': budgets,
    })


@serialized_user_settings_write
async def api_budgets_delete(request: web.Request):
    budget_type = request.match_info.get('type')
    category = unquote(request.match_info.get('category', ''))
    if budget_type not in ('income', 'expense'):
        return _json_response(
            {'detail': 'type must be income or expense'}, status=400
        )
    if not category or len(category) > 80:
        return _json_response({'detail': 'Not found'}, status=404)
    deleted = await db.delete_budget(
        request['user_id'], budget_type, category
    )
    if not deleted:
        return _json_response({'detail': 'Not found'}, status=404)
    return web.Response(status=204)


# ---- recurring operations, insights, digest, forecast ----

def _recurring_payload(row):
    return {
        'id': row['id'],
        'type': row['type'],
        'amount': float(row['amount']),
        'currency': row['currency'],
        'amount_uah': _round_money(row['amount_uah']),
        'category': row['category'],
        'subcategory': row.get('subcategory'),
        'description': row.get('description') or '',
        'payment_source': row.get('payment_source'),
        'frequency': row['frequency'],
        'interval': int(row['interval']),
        'start_date': row['start_date'],
        'next_due_date': row['next_due_date'],
        'last_generated_date': row.get('last_generated_date'),
        'auto_create': bool(row['auto_create']),
        'active': bool(row['active']),
    }


def _next_recurrence_after(start, frequency, interval, anchor_day, baseline):
    if start > baseline:
        return start
    if frequency in ('daily', 'weekly'):
        step_days = interval if frequency == 'daily' else interval * 7
        steps = (baseline - start).days // step_days + 1
        return start + timedelta(days=steps * step_days)
    candidate = start
    for _ in range(5000):
        if candidate > baseline:
            return candidate
        candidate = advance_recurrence(
            candidate, frequency, interval=interval, anchor_day=anchor_day
        )
    raise ValueError('recurring schedule is too old')


def _first_recurrence_on_or_after(
    start, frequency, interval, anchor_day, target
):
    return _next_recurrence_after(
        start,
        frequency,
        interval,
        anchor_day,
        target - timedelta(days=1),
    )


async def _validate_recurring_values(user_id, body, existing=None):
    if not isinstance(body, dict):
        return None, _json_response(
            {'detail': 'JSON body must be an object'}, status=400
        )
    allowed = {
        'type', 'amount', 'currency', 'category', 'subcategory', 'description',
        'payment_source', 'frequency', 'interval', 'start_date', 'auto_create',
        'active',
    }
    unknown = set(body) - allowed
    if unknown:
        return None, _json_response(
            {'detail': f'unsupported fields: {", ".join(sorted(unknown))}'},
            status=400,
        )

    def current(name, default=None):
        if name in body:
            return body[name]
        if existing is not None:
            return existing.get(name, default)
        return default

    recurring_type = current('type')
    if recurring_type not in ('income', 'expense'):
        return None, _json_response(
            {'detail': 'type must be income or expense'}, status=400
        )

    raw_amount = current('amount')
    if raw_amount is None or isinstance(raw_amount, bool):
        return None, _json_response(
            {'detail': 'amount must be a positive number'}, status=400
        )
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        return None, _json_response(
            {'detail': 'amount must be a positive number'}, status=400
        )
    if not math.isfinite(amount) or not 0 < amount <= 1_000_000_000:
        return None, _json_response(
            {'detail': 'amount must be between 0 and 1000000000'}, status=400
        )

    currency = str(current('currency', 'UAH')).upper()
    if currency not in ('UAH', 'USD', 'EUR'):
        return None, _json_response(
            {'detail': 'currency must be UAH, USD, or EUR'}, status=400
        )

    raw_category = current('category')
    if not isinstance(raw_category, str):
        return None, _json_response(
            {'detail': 'category must be a string'}, status=400
        )
    category = raw_category.strip()
    if not category or len(category) > 80:
        return None, _json_response(
            {'detail': 'category must be 1-80 characters'}, status=400
        )

    raw_subcategory = current('subcategory')
    if raw_subcategory is not None and not isinstance(raw_subcategory, str):
        return None, _json_response(
            {'detail': 'subcategory must be a string or null'}, status=400
        )
    subcategory = raw_subcategory.strip() if raw_subcategory else None
    if subcategory and len(subcategory) > 80:
        return None, _json_response(
            {'detail': 'subcategory must be at most 80 characters'}, status=400
        )

    raw_description = current('description', '')
    if raw_description is not None and not isinstance(raw_description, str):
        return None, _json_response(
            {'detail': 'description must be a string'}, status=400
        )
    description = (raw_description or '').strip()
    if len(description) > 200:
        return None, _json_response(
            {'detail': 'description must be at most 200 characters'}, status=400
        )

    source_body = {'payment_source': current('payment_source')}
    payment_source, source_error = _parse_payment_source(source_body)
    if source_error is not None:
        return None, source_error

    frequency = current('frequency')
    if frequency not in SUPPORTED_FREQUENCIES:
        return None, _json_response(
            {'detail': 'frequency must be daily, weekly, monthly, or yearly'},
            status=400,
        )
    interval = current('interval', 1)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 365:
        return None, _json_response(
            {'detail': 'interval must be an integer between 1 and 365'}, status=400
        )

    raw_start = current('start_date')
    try:
        start = date.fromisoformat(str(raw_start))
    except (TypeError, ValueError):
        return None, _json_response(
            {'detail': 'start_date must be YYYY-MM-DD'}, status=400
        )

    auto_create = (
        body['auto_create']
        if 'auto_create' in body
        else bool(existing['auto_create']) if existing is not None else True
    )
    active = (
        body['active']
        if 'active' in body
        else bool(existing['active']) if existing is not None else True
    )
    if not isinstance(auto_create, bool) or not isinstance(active, bool):
        return None, _json_response(
            {'detail': 'auto_create and active must be booleans'}, status=400
        )

    settings = await user_settings_for(user_id)
    category_entry = (
        settings.get('categories', {}).get(recurring_type, {}).get(category)
    )
    if not isinstance(category_entry, dict):
        return None, _json_response({'detail': 'unknown category'}, status=400)
    if subcategory and subcategory not in (category_entry.get('subcategories') or []):
        return None, _json_response({'detail': 'unknown subcategory'}, status=400)

    amount_changed = existing is None or 'amount' in body or 'currency' in body
    if amount_changed:
        try:
            rate = await get_exchange_rate(currency)
        except ExchangeRateUnavailableError:
            return None, _exchange_rate_unavailable_response()
        amount_uah = round(convert_to_uah(amount, currency, rate), 2)
        if amount_uah < 0.01:
            return None, _json_response(
                {'detail': 'UAH equivalent must be at least 0.01'}, status=400
            )
    else:
        amount_uah = float(existing['amount_uah'])

    schedule_fields_changed = bool(
        {'frequency', 'interval', 'start_date'} & set(body)
    )
    today = datetime.now(KYIV_TZ).date()
    reactivating = (
        existing is not None
        and not bool(existing['active'])
        and active
    )
    if existing is None:
        anchor_day = start.day
        last_generated_date = None
        next_due_date = _first_recurrence_on_or_after(
            start, frequency, interval, anchor_day, today
        ).isoformat()
    elif schedule_fields_changed or reactivating:
        anchor_day = start.day if schedule_fields_changed else int(existing['anchor_day'])
        last_generated_date = existing.get('last_generated_date')
        target = today
        if last_generated_date:
            target = max(
                target,
                date.fromisoformat(last_generated_date) + timedelta(days=1),
            )
        next_due_date = _first_recurrence_on_or_after(
            start, frequency, interval, anchor_day, target
        ).isoformat()
    else:
        next_due_date = existing['next_due_date']
        last_generated_date = existing.get('last_generated_date')
        anchor_day = int(existing['anchor_day'])

    return {
        'type': recurring_type,
        'amount': amount,
        'currency': currency,
        'amount_uah': amount_uah,
        'category': category,
        'subcategory': subcategory,
        'description': description,
        'payment_source': payment_source,
        'frequency': frequency,
        'interval': interval,
        'start_date': start.isoformat(),
        'anchor_day': anchor_day,
        'next_due_date': next_due_date,
        'last_generated_date': last_generated_date,
        'auto_create': auto_create,
        'active': active,
    }, None


async def api_recurring_list(request: web.Request):
    rows = await db.list_recurring_operations(request['user_id'])
    return _json_response([_recurring_payload(row) for row in rows])


@serialized_recurring_write
async def api_recurring_create(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    values, err = await _validate_recurring_values(request['user_id'], body)
    if err is not None:
        return err
    row = await db.create_recurring_operation(request['user_id'], values)
    return _json_response(_recurring_payload(row), status=201)


@serialized_recurring_write
async def api_recurring_patch(request: web.Request):
    try:
        recurring_id = int(request.match_info['id'])
    except (KeyError, TypeError, ValueError):
        return _json_response({'detail': 'Invalid id'}, status=400)
    existing = await db.get_recurring_operation(request['user_id'], recurring_id)
    if existing is None:
        return _json_response({'detail': 'Not found'}, status=404)
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    values, err = await _validate_recurring_values(
        request['user_id'], body, existing=existing
    )
    if err is not None:
        return err
    row = await db.update_recurring_operation(
        request['user_id'], recurring_id, values
    )
    if row is None:
        return _json_response({'detail': 'Not found'}, status=404)
    return _json_response(_recurring_payload(row))


@serialized_recurring_write
async def api_recurring_delete(request: web.Request):
    try:
        recurring_id = int(request.match_info['id'])
    except (KeyError, TypeError, ValueError):
        return _json_response({'detail': 'Invalid id'}, status=400)
    deleted = await db.delete_recurring_operation(
        request['user_id'], recurring_id
    )
    if not deleted:
        return _json_response({'detail': 'Not found'}, status=404)
    return web.Response(status=204)


async def process_due_recurring_operations(through_date=None):
    through = through_date or datetime.now(KYIV_TZ).date()
    if not isinstance(through, date):
        through = date.fromisoformat(str(through))
    templates = await db.list_due_recurring_operations(through.isoformat())
    created = failed = processed = 0
    for candidate in templates:
        owner = str(candidate['user_id'])
        async with _recurring_user_locks[owner]:
            try:
                template = await db.get_recurring_operation(
                    owner, candidate['id']
                )
                if (
                    template is None
                    or not bool(template['active'])
                    or not bool(template['auto_create'])
                    or template['next_due_date'] > through.isoformat()
                ):
                    continue
                first_due = date.fromisoformat(template['next_due_date'])
                due_dates = due_recurrence_dates(
                    start_date=first_due,
                    through=through,
                    frequency=template['frequency'],
                    interval=int(template['interval']),
                    anchor_day=int(template['anchor_day']),
                )
                if not due_dates:
                    continue
                next_due = advance_recurrence(
                    due_dates[-1],
                    template['frequency'],
                    interval=int(template['interval']),
                    anchor_day=int(template['anchor_day']),
                )
                rate = await get_exchange_rate(template['currency'])
                materialized = await db.materialize_recurring_occurrences(
                    owner,
                    template['id'],
                    expected_next_due=template['next_due_date'],
                    expected_currency=template['currency'],
                    due_dates=due_dates,
                    rate=rate,
                    next_due_date=next_due,
                )
                created += materialized['created']
                processed += int(materialized['processed'])
            except Exception as exc:
                failed += 1
                logger.exception(
                    'recurring operation failed id=%s: %s',
                    candidate.get('id'),
                    exc,
                )
    return {'created': created, 'failed': failed, 'processed': processed}


async def api_recurring_suggestions(request: web.Request):
    user_id = request['user_id']
    rows = [
        row for row in await db.get_transactions(user_id)
        if not str(row.get('client_request_id') or '').startswith('recurring:')
    ]
    existing = await db.list_recurring_operations(user_id)

    def identity(row):
        try:
            amount = Decimal(str(row.get('amount'))).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, TypeError, ValueError):
            amount = None
        return (
            row.get('type'),
            str(row.get('category') or ''),
            str(row.get('subcategory') or ''),
            amount,
            str(row.get('currency') or 'UAH').upper(),
            str(row.get('payment_source') or ''),
            ' '.join(str(row.get('description') or '').split()).casefold(),
        )

    existing_identities = {identity(row) for row in existing}
    suggestions = [
        candidate for candidate in detect_recurring_candidates(rows)
        if identity(candidate) not in existing_identities
    ]
    return _json_response(suggestions)


def _query_date(request, field, default):
    raw = request.rel_url.query.get(field)
    if raw is None:
        return default, None
    try:
        return date.fromisoformat(raw), None
    except (TypeError, ValueError):
        return None, _json_response(
            {'detail': f'{field} must be YYYY-MM-DD'}, status=400
        )


async def api_insights(request: web.Request):
    today, err = _query_date(
        request, 'as_of', datetime.now(KYIV_TZ).date()
    )
    if err is not None:
        return err
    rows = await db.get_transactions(request['user_id'])
    budgets = [
        budget for budget in await db.get_budgets(request['user_id'])
        if budget.get('type') == 'expense'
    ]
    return _json_response(list(build_financial_insights(
        rows, budgets=budgets, today=today
    )))


async def api_weekly_digest(request: web.Request):
    today = datetime.now(KYIV_TZ).date()
    default_start = today - timedelta(days=today.weekday())
    week_start, err = _query_date(request, 'week_start', default_start)
    if err is not None:
        return err
    if week_start.weekday() != 0:
        return _json_response(
            {'detail': 'week_start must be a Monday'}, status=400
        )
    rows = await db.get_transactions(request['user_id'])
    return _json_response(build_weekly_digest(rows, week_start=week_start))


async def _scheduled_occurrences_for_month(
    recurring_rows, *, year, month, as_of
):
    _, month_end_text = _month_date_bounds(year, month)
    month_end = date.fromisoformat(month_end_text) - timedelta(days=1)
    result = []
    rates = {'UAH': 1.0}
    for row in recurring_rows:
        if not bool(row['active']):
            continue
        start = date.fromisoformat(row['next_due_date'])
        due_dates = due_recurrence_dates(
            start_date=start,
            through=month_end,
            frequency=row['frequency'],
            interval=int(row['interval']),
            anchor_day=int(row['anchor_day']),
        )
        for due_date in due_dates:
            if due_date <= as_of or due_date.year != year or due_date.month != month:
                continue
            currency = row['currency']
            if currency not in rates:
                rates[currency] = await get_exchange_rate(currency)
            result.append({
                'date': due_date.isoformat(),
                'type': row['type'],
                'amount_uah': round(convert_to_uah(
                    float(row['amount']), currency, rates[currency]
                ), 2),
            })
    return result


async def api_forecast(request: web.Request):
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err
    as_of, err = _query_date(
        request, 'as_of', datetime.now(KYIV_TZ).date()
    )
    if err is not None:
        return err
    rows = await db.get_transactions(request['user_id'], year=year, month=month)
    recurring = await db.list_recurring_operations(request['user_id'])
    try:
        scheduled = await _scheduled_occurrences_for_month(
            recurring, year=year, month=month, as_of=as_of
        )
    except ExchangeRateUnavailableError:
        return _exchange_rate_unavailable_response()
    projected_income = sum(
        Decimal(str(row['amount_uah']))
        for row in (*rows, *scheduled) if row['type'] == 'income'
    )
    settings = await user_settings_for(request['user_id'])
    try:
        tax = calculate_tax_group(
            projected_income, settings.get('tax_config'), year=year
        )
    except ValueError as exc:
        return _json_response({'detail': str(exc)}, status=422)
    return _json_response(forecast_month_result(
        rows,
        scheduled,
        year=year,
        month=month,
        estimated_tax_uah=tax['total_tax'],
    ))


async def api_notification_settings(request: web.Request):
    return _json_response(
        await db.get_notification_preferences(request['user_id'])
    )


@serialized_user_settings_write
async def api_notification_settings_patch(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    if not isinstance(body, dict) or set(body) != {'weekly_digest_enabled'}:
        return _json_response(
            {'detail': 'weekly_digest_enabled is required'}, status=400
        )
    enabled = body['weekly_digest_enabled']
    if not isinstance(enabled, bool):
        return _json_response(
            {'detail': 'weekly_digest_enabled must be boolean'}, status=400
        )
    return _json_response(await db.set_notification_preferences(
        request['user_id'], enabled
    ))


def _weekly_digest_message(digest):
    return (
        '📊 Тижневий дайджест Ruby Finance\n\n'
        f'Доходи: {digest["total_income"]} ₴\n'
        f'Витрати: {digest["total_expense"]} ₴\n'
        f'Чистий результат: {digest["net"]} ₴'
    )


async def send_weekly_digests(telegram_bot, *, week_start=None):
    start = week_start or (
        datetime.now(KYIV_TZ).date()
        - timedelta(days=datetime.now(KYIV_TZ).date().weekday())
    )
    if not isinstance(start, date):
        start = date.fromisoformat(str(start))
    if start.weekday() != 0:
        raise ValueError('week_start must be a Monday')
    period_key = f'{start.isocalendar().year}-W{start.isocalendar().week:02d}'
    sent = failed = skipped = 0
    for user_id in await db.list_weekly_digest_users():
        async with _recurring_user_locks[str(user_id)]:
            preferences = await db.get_notification_preferences(user_id)
            if not preferences['weekly_digest_enabled']:
                continue
            claimed = await db.claim_notification_delivery(
                user_id, 'weekly_digest', period_key
            )
            if not claimed:
                skipped += 1
                continue
            try:
                rows = await db.get_transactions(user_id)
                digest = build_weekly_digest(rows, week_start=start)
                message = await telegram_bot.send_message(
                    chat_id=user_id, text=_weekly_digest_message(digest)
                )
                await db.finish_notification_delivery(
                    user_id,
                    'weekly_digest',
                    period_key,
                    'sent',
                    message_id=getattr(message, 'message_id', None),
                )
                sent += 1
            except Exception as exc:
                await db.finish_notification_delivery(
                    user_id, 'weekly_digest', period_key, 'failed',
                    error=str(exc)[:200],
                )
                failed += 1
    return {'sent': sent, 'failed': failed, 'skipped': skipped}


async def recurring_operations_job(context: ContextTypes.DEFAULT_TYPE):
    result = await process_due_recurring_operations()
    logger.info('recurring scheduled job complete: %s', result)


async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE, *, today=None):
    # Registered daily for timezone/DST safety; Sunday sends the current week.
    current_day = today or datetime.now(KYIV_TZ).date()
    if current_day.weekday() != 6:
        return
    result = await send_weekly_digests(
        context.bot, week_start=current_day - timedelta(days=6)
    )
    logger.info('weekly digest scheduled job complete: %s', result)


# ---- reports parity ----

async def api_report_employees(request: web.Request):
    """Mirror show_employee_report: per-employee income/salary/profit/ROI."""
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err

    transactions = await db.get_transactions(user_id, year=year, month=month)
    user_settings = await user_settings_for(user_id)
    user_employees = employee_names_for_report(user_settings, transactions)

    employees = []
    for emp in user_employees:
        income_cat = f'Від {emp}'
        salary_cat = f'ЗП {emp}'
        income = sum(t['amount_uah'] for t in transactions
                     if t['type'] == 'income' and t['category'] == income_cat)
        salary = sum(t['amount_uah'] for t in transactions
                     if t['type'] == 'expense' and t['category'] == salary_cat)
        if income > 0 or salary > 0:
            profit = income - salary
            roi = ((income - salary) / salary * 100) if salary > 0 else None
            employees.append({
                'name': emp,
                'income': round(income, 2),
                'salary': round(salary, 2),
                'profit': round(profit, 2),
                'roi': round(roi, 2) if roi is not None else None,
            })

    return _json_response(employees)


async def api_report_tax(request: web.Request):
    """Return year-versioned standard FOP tax estimates for one month."""
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err

    transactions = await db.get_transactions(user_id, year=year, month=month)
    total_income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense')
    profit = total_income - total_expense

    user_settings = await user_settings_for(user_id)
    user_tax = user_settings.get('tax_config', DEFAULT_SETTINGS['tax_config'])
    try:
        tax = calculate_tax_group(total_income, user_tax, year=year)
    except ValueError as exc:
        return _json_response({'detail': str(exc)}, status=422)

    import calendar
    last_day = calendar.monthrange(year, month)[1]
    period_from = f"{year:04d}-{month:02d}-01"
    period_to = f"{year:04d}-{month:02d}-{last_day:02d}"

    return _json_response({
        'year': year,
        'month': month,
        'month_name': MONTH_NAMES[month],
        'group': tax['group'],
        'group_label': tax['group_label'],
        'scheme': tax['scheme'],
        'scheme_label': tax['scheme_label'],
        'total_income': round(total_income, 2),
        'total_expense': round(total_expense, 2),
        'profit': round(profit, 2),
        'single_tax_rate': tax['single_tax_rate'],
        'esv_fixed': round(tax['esv'], 2),
        'single_tax': round(tax['single_tax'], 2),
        'military_levy': round(tax['military_levy'], 2),
        'total_tax': round(tax['total_tax'], 2),
        'after_tax': round(profit - tax['total_tax'], 2),
        'vat_registered': tax['vat_registered'],
        'vat_included': tax['vat_included'],
        'rules_year': tax['rules_year'],
        'disclaimer': tax['disclaimer'],
        'period_from': period_from,
        'period_to': period_to,
    })


async def api_report_accounting(request: web.Request):
    """Mirror show_accounting_report: opening/closing balance + Dt/Ct entries."""
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err

    transactions = await db.get_transactions(user_id, year=year, month=month)
    total_income = sum(t['amount_uah'] for t in transactions if t['type'] == 'income')
    total_expense = sum(t['amount_uah'] for t in transactions if t['type'] == 'expense')
    profit = total_income - total_expense

    prev_transactions = await db.get_all_transactions(user_id)
    prev_income = 0.0
    prev_expense = 0.0
    month_start = datetime(year, month, 1)
    for t in prev_transactions:
        t_date = datetime.strptime(t['date'], '%Y-%m-%d')
        if t_date < month_start:
            if t['type'] == 'income':
                prev_income += t['amount_uah']
            else:
                prev_expense += t['amount_uah']

    opening_balance = prev_income - prev_expense
    closing_balance = opening_balance + profit

    source_summary = _payment_source_summary(transactions)
    entries = _simplified_accounting_entries(transactions)

    return _json_response({
        'total_income': round(total_income, 2),
        'total_expense': round(total_expense, 2),
        'profit': round(profit, 2),
        'opening_balance': round(opening_balance, 2),
        'closing_balance': round(closing_balance, 2),
        **source_summary,
        'entries': entries,
        'result': 'profit' if profit > 0 else 'loss',
        'model': ACCOUNTING_MODEL,
        'disclaimer': ACCOUNTING_DISCLAIMER,
    })


async def api_report_time(request: web.Request):
    """Mirror show_time_monthly_report: per-category time + productivity buckets."""
    user_id = request['user_id']
    year, month, err = _parse_year_month(request)
    if err is not None:
        return err

    time_tracks = await db.get_time_tracks(user_id, year=year, month=month)

    time_by_cat: dict[str, int] = {}
    total_minutes = 0
    for track in time_tracks:
        total_minutes += track['minutes']
        time_by_cat[track['category']] = time_by_cat.get(track['category'], 0) + track['minutes']

    import calendar
    days_in_month = calendar.monthrange(year, month)[1]
    total_hours = total_minutes / 60
    avg_per_day = total_hours / days_in_month if days_in_month else 0

    productive_cats = ['Робота', 'Навчання', 'Підвищення кваліфікації',
                       'Уроки історії', 'Уроки англійської', 'Зал']
    unproductive_cats = ['Скрол стрічки', 'Розваги']
    rest_cats = ['Сон', 'Їжа', 'Відпустка']

    productive_minutes = sum(time_by_cat.get(c, 0) for c in productive_cats)
    unproductive_minutes = sum(time_by_cat.get(c, 0) for c in unproductive_cats)
    rest_minutes = sum(time_by_cat.get(c, 0) for c in rest_cats)
    untracked_minutes = max(0, days_in_month * 24 * 60 - total_minutes)

    user_settings = await user_settings_for(user_id)
    user_time_cats = user_settings.get('time_categories', {}) or {}

    by_category = []
    for cat, minutes in sorted(time_by_cat.items(), key=lambda x: x[1], reverse=True):
        emoji = (user_time_cats.get(cat) or {}).get('emoji', '⏱️')
        pct = (minutes / total_minutes * 100) if total_minutes > 0 else 0
        by_category.append({
            'name': cat,
            'emoji': emoji,
            'minutes': minutes,
            'hours': round(minutes / 60, 2),
            'percentage': round(pct, 2),
        })

    return _json_response({
        'total_minutes': total_minutes,
        'total_hours': round(total_hours, 2),
        'days_in_month': days_in_month,
        'avg_per_day_hours': round(avg_per_day, 2),
        'by_category': by_category,
        'productive_minutes': productive_minutes,
        'unproductive_minutes': unproductive_minutes,
        'rest_minutes': rest_minutes,
        'untracked_minutes': untracked_minutes,
    })


# ---- categories CRUD (per-user) ----

async def api_categories_full(request: web.Request):
    """Return THIS user's CATEGORIES dict, normalised so every category
    entry always carries a `subcategories` list (default categories that
    were never edited omit the key in storage — consumers shouldn't have to
    guard for its absence)."""
    user_settings = await user_settings_for(request['user_id'])
    cats = user_settings.get('categories', {}) or {}
    for cat_type in ('expense', 'income'):
        for name, entry in (cats.get(cat_type, {}) or {}).items():
            if isinstance(entry, dict) and not isinstance(entry.get('subcategories'), list):
                entry['subcategories'] = []
    return _json_response(cats)


@serialized_user_settings_write
async def api_categories_create(request: web.Request):
    user_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    cat_type = body.get('type')
    if cat_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)

    name = _clean_text(body.get('name'), max_len=80)
    if not name:
        return _json_response({'detail': 'name required'}, status=400)
    if len(str(body.get('name') or '').strip()) > 80:
        return _json_response({'detail': 'name must be at most 80 characters'}, status=400)
    if _is_employee_category_namespace(cat_type, name):
        return _json_response(
            {'detail': 'employee category namespace is reserved'}, status=400
        )

    settings = await user_settings_for(user_id)
    bucket = settings.setdefault('categories', {}).setdefault(cat_type, {})
    if name in bucket:
        return _json_response({'detail': 'category already exists'}, status=409)

    raw_subcategories = body.get('subcategories') or []
    if not isinstance(raw_subcategories, list):
        return _json_response({'detail': 'subcategories must be a list'}, status=400)
    subcategories = list(dict.fromkeys(
        cleaned for item in raw_subcategories
        if isinstance(item, str) and (cleaned := _clean_text(item, max_len=80))
    ))
    if len(subcategories) > 30:
        return _json_response({'detail': 'too many subcategories (max 30)'}, status=400)
    keywords = body.get('keywords', []) or []
    if not isinstance(keywords, list) or not all(isinstance(k, str) for k in keywords):
        return _json_response({'detail': 'keywords must be a list of strings'}, status=400)
    entry = {
        'emoji': _clean_text(body.get('emoji'), max_len=16, default='📦'),
        'keywords': keywords[:30],
        'subcategories': subcategories,
    }
    bucket[name] = entry
    await save_user_settings(user_id, settings)
    return _json_response({'type': cat_type, 'name': name, **entry}, status=201)


@serialized_user_settings_write
async def api_categories_update(request: web.Request):
    user_id = request['user_id']
    cat_type = request.match_info.get('type')
    name = unquote(request.match_info.get('name', ''))
    if cat_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)
    if _is_employee_category_namespace(cat_type, name):
        return _json_response(
            {'detail': 'generated employee categories are managed via employees'},
            status=400,
        )

    settings = _copy.deepcopy(await user_settings_for(user_id))
    bucket = settings.get('categories', {}).get(cat_type, {})
    if name not in bucket:
        return _json_response({'detail': 'category not found'}, status=404)

    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    current = bucket[name]
    new_emoji = _clean_text(
        body.get('emoji', current.get('emoji', '📦')), max_len=16, default='📦')
    new_keywords = body.get('keywords', current.get('keywords', []))
    # Preserve subcategories across rename/update unless explicitly provided.
    new_subs = body.get('subcategories', current.get('subcategories', []))
    raw_new_name = str(body.get('new_name') or name).strip()
    if len(raw_new_name) > 80:
        return _json_response({'detail': 'new_name must be at most 80 characters'}, status=400)
    new_name = raw_new_name or name
    if _is_employee_category_namespace(cat_type, new_name):
        return _json_response(
            {'detail': 'employee category namespace is reserved'}, status=400
        )

    if not isinstance(new_keywords, list) or not all(isinstance(k, str) for k in new_keywords):
        return _json_response({'detail': 'keywords must be a list of strings'}, status=400)
    if not isinstance(new_subs, list):
        return _json_response({'detail': 'subcategories must be a list'}, status=400)
    clean_subs = list(dict.fromkeys(
        cleaned for item in new_subs
        if isinstance(item, str) and (cleaned := _clean_text(item, max_len=80))
    ))
    if len(clean_subs) > 30:
        return _json_response({'detail': 'too many subcategories (max 30)'}, status=400)

    if new_name != name and new_name in bucket:
        return _json_response({'detail': 'target name already exists'}, status=409)
    if name == 'Інше' and new_name != 'Інше':
        return _json_response({'detail': 'cannot rename "Інше"'}, status=400)

    new_entry = {
        'emoji': new_emoji,
        'keywords': new_keywords[:30],
        'subcategories': clean_subs,
    }
    removed_subcategories = tuple(
        subcategory
        for subcategory in current.get('subcategories', [])
        if subcategory not in clean_subs
    )
    new_bucket = {}
    for k, v in list(bucket.items()):
        new_bucket[new_name if k == name else k] = new_entry if k == name else v
    if len(new_bucket) != len(bucket):
        return _json_response({'detail': 'rename collision'}, status=409)
    settings['categories'][cat_type] = new_bucket
    normalize_tax_config(settings)
    rebuild_user_categories(settings)
    try:
        await db.save_category_rename(
            user_id,
            cat_type,
            name,
            new_name,
            settings,
            removed_subcategories=removed_subcategories,
        )
    except sqlite3.IntegrityError:
        return _json_response(
            {'detail': 'category dependency conflict'}, status=409
        )
    return _json_response({'type': cat_type, 'name': new_name, **new_entry})


@serialized_user_settings_write
async def api_categories_delete(request: web.Request):
    user_id = request['user_id']
    cat_type = request.match_info.get('type')
    name = unquote(request.match_info.get('name', ''))
    if cat_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)
    if _is_employee_category_namespace(cat_type, name):
        return _json_response(
            {'detail': 'delete the employee instead of its generated category'},
            status=400,
        )
    if name == 'Інше':
        return _json_response({'detail': 'cannot delete "Інше"'}, status=400)

    settings = _copy.deepcopy(await user_settings_for(user_id))
    bucket = settings.get('categories', {}).get(cat_type, {})
    if name not in bucket:
        return _json_response({'detail': 'category not found'}, status=404)

    del bucket[name]
    normalize_tax_config(settings)
    rebuild_user_categories(settings)
    try:
        await db.save_category_delete(user_id, cat_type, name, settings)
    except sqlite3.IntegrityError:
        return _json_response(
            {'detail': 'category dependency conflict'}, status=409
        )
    return web.Response(status=204)


# ---- subcategories CRUD (per-user, nested under a category) ----

@serialized_user_settings_write
async def api_subcategories_create(request: web.Request):
    """POST /api/categories/{type}/{name}/subcategories  body {name}"""
    user_id = request['user_id']
    cat_type = request.match_info.get('type')
    cat_name = unquote(request.match_info.get('name', ''))
    if cat_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)

    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    sub_name = _clean_text(body.get('name'), max_len=80)
    if not sub_name:
        return _json_response({'detail': 'subcategory name required'}, status=400)

    settings = _copy.deepcopy(await user_settings_for(user_id))
    bucket = settings.get('categories', {}).get(cat_type, {})
    if cat_name not in bucket:
        return _json_response({'detail': 'category not found'}, status=404)

    entry = bucket[cat_name]
    subs = entry.setdefault('subcategories', [])
    if sub_name in subs:
        return _json_response({'detail': 'subcategory already exists'}, status=409)
    if len(subs) >= 30:
        return _json_response({'detail': 'too many subcategories (max 30)'}, status=400)

    subs.append(sub_name)
    await save_user_settings(user_id, settings)
    return _json_response({'category': cat_name, 'subcategory': sub_name}, status=201)


@serialized_user_settings_write
async def api_subcategories_delete(request: web.Request):
    """DELETE /api/categories/{type}/{name}/subcategories/{sub}"""
    user_id = request['user_id']
    cat_type = request.match_info.get('type')
    cat_name = unquote(request.match_info.get('name', ''))
    sub_name = unquote(request.match_info.get('sub', ''))
    if cat_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)

    settings = _copy.deepcopy(await user_settings_for(user_id))
    bucket = settings.get('categories', {}).get(cat_type, {})
    if cat_name not in bucket:
        return _json_response({'detail': 'category not found'}, status=404)

    subs = bucket[cat_name].get('subcategories', [])
    if sub_name not in subs:
        return _json_response({'detail': 'subcategory not found'}, status=404)

    subs.remove(sub_name)
    await db.save_subcategory_delete(
        user_id, cat_type, cat_name, sub_name, settings
    )
    return web.Response(status=204)


async def api_report_category_breakdown(request: web.Request):
    """GET /api/reports/category-breakdown?type=&category=&period=...
    Aggregate ONE category's transactions by subcategory for the window.
    Rows with no subcategory bucket into «Без підрозділу»."""
    user_id = request['user_id']
    q = request.rel_url.query

    cat_type = q.get('type')
    if cat_type not in ('income', 'expense'):
        return _json_response({'detail': 'type must be income or expense'}, status=400)
    category = _clean_text(q.get('category'), max_len=80)
    if not category:
        return _json_response({'detail': 'category required'}, status=400)

    # Reuse the same period/date logic as the transaction list
    from_date = q.get('from')
    to_date = q.get('to')
    period = q.get('period')
    now = datetime.now(KYIV_TZ)
    import calendar
    if not from_date and not to_date:
        if period == '10d':
            from datetime import timedelta
            from_date = (now - timedelta(days=9)).strftime('%Y-%m-%d')
            to_date = now.strftime('%Y-%m-%d')
        elif period == '30d':
            from datetime import timedelta
            from_date = (now - timedelta(days=29)).strftime('%Y-%m-%d')
            to_date = now.strftime('%Y-%m-%d')
        elif period == 'month':
            year_v, month_v, err = _parse_year_month(request)
            if err is not None:
                return err
            last_day = calendar.monthrange(year_v, month_v)[1]
            from_date = f'{year_v:04d}-{month_v:02d}-01'
            to_date = f'{year_v:04d}-{month_v:02d}-{last_day:02d}'
        else:  # default current month
            last_day = calendar.monthrange(now.year, now.month)[1]
            from_date = f'{now.year:04d}-{now.month:02d}-01'
            to_date = f'{now.year:04d}-{now.month:02d}-{last_day:02d}'

    for key, value in (('from', from_date), ('to', to_date)):
        if value and not _looks_like_iso_date(value):
            return _json_response({'detail': f'{key} must be YYYY-MM-DD'}, status=400)
    if from_date and to_date and from_date > to_date:
        return _json_response({'detail': 'from must not be after to'}, status=400)

    rows = await db.get_transactions(
        user_id, t_type=cat_type, from_date=from_date, to_date=to_date,
    )
    rows = [r for r in rows if r['category'] == category]

    by_sub: dict[str, float] = {}
    total = 0.0
    for r in rows:
        v = r['amount_uah'] or 0
        sub = (r['subcategory'] if 'subcategory' in r.keys() else None) or 'Без підрозділу'
        by_sub[sub] = round(by_sub.get(sub, 0.0) + v, 2)
        total += v

    breakdown = [
        {'name': k, 'value': v, 'percentage': round((v / total * 100) if total else 0, 1)}
        for k, v in sorted(by_sub.items(), key=lambda x: x[1], reverse=True)
    ]
    return _json_response({
        'category': category,
        'type': cat_type,
        'total': round(total, 2),
        'breakdown': breakdown,
        'from': from_date,
        'to': to_date,
    })


# ---- employees CRUD (per-user) ----

async def api_employees_list(request: web.Request):
    settings = await user_settings_for(request['user_id'])
    return _json_response(settings.get('employees', []))


@serialized_user_settings_write
async def api_employees_create(request: web.Request):
    user_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    name = _clean_text(body.get('name'), max_len=60)
    if not name:
        return _json_response({'detail': 'name required'}, status=400)

    settings = await user_settings_for(user_id)
    employees_list = settings.setdefault('employees', [])
    if name in employees_list:
        return _json_response({'detail': 'employee already exists'}, status=409)

    employees_list.append(name)
    await save_user_settings(user_id, settings)  # rebuilds employee categories
    return _json_response({'name': name}, status=201)


@serialized_user_settings_write
async def api_employees_delete(request: web.Request):
    user_id = request['user_id']
    name = unquote(request.match_info.get('name', ''))

    if not await _delete_employee_locked(user_id, name):
        return _json_response({'detail': 'employee not found'}, status=404)
    return web.Response(status=204)


# ---- time categories CRUD (per-user) ----

async def api_time_categories_list(request: web.Request):
    settings = await user_settings_for(request['user_id'])
    return _json_response(settings.get('time_categories', {}))


@serialized_user_settings_write
async def api_time_categories_create(request: web.Request):
    user_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    name = _clean_text(body.get('name'), max_len=60)
    if not name:
        return _json_response({'detail': 'name required'}, status=400)

    settings = await user_settings_for(user_id)
    bucket = settings.setdefault('time_categories', {})
    if name in bucket:
        return _json_response({'detail': 'time category already exists'}, status=409)

    entry = {'emoji': body.get('emoji', '⏱️')}
    bucket[name] = entry
    await save_user_settings(user_id, settings)
    return _json_response({'name': name, **entry}, status=201)


@serialized_user_settings_write
async def api_time_categories_delete(request: web.Request):
    user_id = request['user_id']
    name = unquote(request.match_info.get('name', ''))
    if name == 'Інше':
        return _json_response({'detail': 'cannot delete "Інше"'}, status=400)

    settings = await user_settings_for(user_id)
    bucket = settings.setdefault('time_categories', {})
    if name not in bucket:
        return _json_response({'detail': 'time category not found'}, status=404)

    del bucket[name]
    await save_user_settings(user_id, settings)
    return web.Response(status=204)


# ---- time tracks ----

async def api_time_tracks_list(request: web.Request):
    user_id = request['user_id']
    year_raw = request.rel_url.query.get('year')
    month_raw = request.rel_url.query.get('month')
    year_val = month_val = None
    if year_raw or month_raw:
        # both required together if either is present
        y, m, err = _parse_year_month(request)
        if err is not None:
            return err
        year_val, month_val = y, m
    limit_val, err = _parse_limit(request, default=500, hard_cap=500)
    if err is not None:
        return err

    rows = await db.get_time_tracks(user_id, year=year_val, month=month_val, limit=limit_val)
    return _json_response([dict(r) for r in rows])


def _time_track_write_response(row: dict, *, duplicate: bool):
    client_request_id = row.get('client_request_id')
    return {
        'id': row['id'],
        'user_id': str(row['user_id']),
        'minutes': int(row['minutes']),
        'category': row['category'],
        'description': row.get('description') or '',
        'date': row['date'],
        'timestamp': row['timestamp'],
        'client_request_id': client_request_id,
        'duplicate': duplicate,
        'idempotent': client_request_id is not None,
    }


def _time_track_payload_matches(row, *, minutes, category, description):
    return (
        int(row['minutes']) == minutes
        and row['category'] == category
        and (row.get('description') or '') == description
    )


async def api_time_tracks_create(request: web.Request):
    user_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    if not isinstance(body, dict):
        return _json_response({'detail': 'JSON body must be an object'}, status=400)

    client_request_id, err = _parse_client_request_id(body)
    if err is not None:
        return err

    raw_minutes = body.get('minutes')
    if not isinstance(raw_minutes, int) or isinstance(raw_minutes, bool):
        return _json_response({'detail': 'minutes required and must be an integer'}, status=400)
    minutes = raw_minutes
    if minutes <= 0:
        return _json_response({'detail': 'minutes must be positive'}, status=400)
    if minutes > 24 * 60:
        return _json_response({'detail': 'minutes cannot exceed 1440 (24 h)'}, status=400)

    category = _clean_text(body.get('category'), max_len=60)
    if not category:
        return _json_response({'detail': 'category required'}, status=400)
    description = _clean_text(body.get('description'), max_len=200)

    if client_request_id is not None:
        existing = await db.get_time_track_by_client_request_id(
            user_id, client_request_id
        )
        if existing is not None:
            if not _time_track_payload_matches(
                existing,
                minutes=minutes,
                category=category,
                description=description,
            ):
                return _idempotency_conflict_response()
            return _json_response(
                _time_track_write_response(existing, duplicate=True)
            )

    # Whitelist against THIS user's own time categories.
    user_settings = await user_settings_for(user_id)
    known_time_cats = set(user_settings.get('time_categories') or {})
    if known_time_cats and category not in known_time_cats:
        return _json_response(
            {'detail': f'unknown time category "{category}"'}, status=400)

    now = datetime.now(KYIV_TZ)
    date_str = now.strftime('%Y-%m-%d')
    ts_str = now.strftime('%Y-%m-%d %H:%M:%S')

    try:
        row_id = await db.add_time_track(
            user_id,
            minutes,
            category,
            description,
            date_str,
            ts_str,
            client_request_id=client_request_id,
        )
    except sqlite3.IntegrityError:
        if client_request_id is None:
            raise
        existing = await db.get_time_track_by_client_request_id(
            user_id, client_request_id
        )
        if existing is None:
            raise
        if not _time_track_payload_matches(
            existing,
            minutes=minutes,
            category=category,
            description=description,
        ):
            return _idempotency_conflict_response()
        return _json_response(
            _time_track_write_response(existing, duplicate=True)
        )
    logger.info("API POST /api/time-tracks user=%s id=%s", user_id, row_id)
    created = {
        'id': row_id,
        'user_id': user_id,
        'minutes': minutes,
        'category': category,
        'description': description,
        'date': date_str,
        'timestamp': ts_str,
        'client_request_id': client_request_id,
    }
    return _json_response(
        _time_track_write_response(created, duplicate=False), status=201
    )


async def api_time_tracks_delete(request: web.Request):
    user_id = request['user_id']
    try:
        track_id = int(request.match_info['id'])
    except (KeyError, ValueError):
        return _json_response({'detail': 'Invalid id'}, status=400)

    deleted = await db.delete_time_track(track_id, user_id=user_id)
    if not deleted:
        return _json_response({'detail': 'Not found'}, status=404)
    logger.info(f"API DELETE /api/time-tracks/{track_id} user={user_id}")
    return web.Response(status=204)


# ---- privacy / account deletion ----

ACCOUNT_DELETE_CONFIRMATION = 'ВИДАЛИТИ'


async def api_account_delete(request: web.Request):
    """Permanently delete the authenticated user's complete account data."""
    user_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)
    confirmation = body.get('confirmation') if isinstance(body, dict) else None
    if not isinstance(confirmation, str) or confirmation.strip() != ACCOUNT_DELETE_CONFIRMATION:
        return _json_response(
            {'detail': f'confirmation must be exactly {ACCOUNT_DELETE_CONFIRMATION}'},
            status=400,
        )

    result = await db.delete_user_account(user_id)
    logger.info('account data deleted for authenticated user=%s', user_id)
    return _json_response({'ok': True, **result})


# ---- tax settings ----

@serialized_user_settings_write
async def api_settings_tax_update(request: web.Request):
    user_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    settings = await user_settings_for(user_id)
    try:
        rules_year = int(body.get('year', CURRENT_TAX_RULES_YEAR))
        _tax_rules_for_year(rules_year)
    except (TypeError, ValueError) as exc:
        return _json_response({'detail': str(exc)}, status=400)

    def finite_number(value, field):
        import math
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, _json_response({'detail': f'{field} must be a number'}, status=400)
        if not math.isfinite(number):
            return None, _json_response({'detail': f'{field} must be finite'}, status=400)
        return number, None

    changes = {}
    if 'group' in body:
        group = str(body['group']).strip().lower()
        if group not in _TAX_GROUPS:
            return _json_response(
                {'detail': 'group must be one of: fop1, fop2, fop3, none'}, status=400)
        changes['group'] = group

    if 'scheme' in body:
        scheme = str(body['scheme']).strip().lower()
        if scheme not in _TAX_SCHEMES:
            return _json_response(
                {'detail': 'scheme must be one of: 5_percent, 3_percent_vat'}, status=400)
        changes['scheme'] = scheme

    if 'single_tax_rate' in body:
        rate, err = finite_number(body['single_tax_rate'], 'single_tax_rate')
        if err is not None:
            return err
        if rate not in (0.03, 0.05):
            return _json_response(
                {'detail': 'single_tax_rate must be 0.05 or 0.03'}, status=400)
        rate_scheme = '3_percent_vat' if rate == 0.03 else '5_percent'
        if 'scheme' in changes and changes['scheme'] != rate_scheme:
            return _json_response({'detail': 'scheme conflicts with single_tax_rate'}, status=400)
        changes['scheme'] = rate_scheme

    if 'fop1_fixed' in body:
        v, err = finite_number(body['fop1_fixed'], 'fop1_fixed')
        if err is not None:
            return err
        if v < 0 or v > 10000:
            return _json_response({'detail': 'fop1_fixed must be between 0 and 10000'}, status=400)
        changes['fop1_fixed'] = v

    if 'fop2_fixed' in body:
        v, err = finite_number(body['fop2_fixed'], 'fop2_fixed')
        if err is not None:
            return err
        if v < 0 or v > 20000:
            return _json_response({'detail': 'fop2_fixed must be between 0 and 20000'}, status=400)
        changes['fop2_fixed'] = v

    if 'esv_fixed' in body:
        esv, err = finite_number(body['esv_fixed'], 'esv_fixed')
        if err is not None:
            return err
        if esv < 0 or esv > 50000:
            return _json_response(
                {'detail': 'esv_fixed must be between 0 and 50000 UAH'}, status=400)
        changes['esv_fixed'] = esv

    profile = update_tax_profile(settings, rules_year, changes)
    await save_user_settings(user_id, settings)
    return _json_response(profile)


async def api_admin_broadcast(request: web.Request):
    """POST /api/admin/broadcast  body {text}
    Admin-only mass message to every registered user. Same effect as the
    in-chat /broadcast command, but reachable over HTTP so an operator
    tool can trigger it. Sends via the Telegram HTTP API directly (no need
    for the Application instance). Skips obvious test ids and tolerates
    'chat not found' for stale entries."""
    if not is_admin(request['user_id']):
        return _json_response({'detail': 'admin only'}, status=403)
    admin_id = request['user_id']
    try:
        body = await request.json()
    except Exception:
        return _json_response({'detail': 'Invalid JSON'}, status=400)

    text = (body.get('text') or '').strip()
    if not text:
        return _json_response({'detail': 'text required'}, status=400)
    if len(text) > 4096:
        return _json_response({'detail': 'text cannot exceed 4096 characters'}, status=400)

    if body.get('confirm') is not True:
        user_ids = await db.get_all_user_ids()
        skipped = sum(str(uid).startswith('999000') for uid in user_ids)
        confirmation_token, expires_at = _issue_broadcast_confirmation(
            admin_id,
            text,
            recipient_ids=user_ids,
        )
        return _json_response({
            'preview': True,
            'confirmation_token': confirmation_token,
            'expires_at': expires_at,
            'text_sha256': _broadcast_text_digest(text),
            'text_length': len(text),
            'total_users': len(user_ids),
            'eligible': len(user_ids) - skipped,
            'skipped': skipped,
        })

    confirmation_token = str(body.get('confirmation_token') or '')
    if not _broadcast_confirmation_matches(admin_id, text, confirmation_token):
        return _json_response(
            {'detail': 'invalid, expired, or already used confirmation token'},
            status=400,
        )
    preview_record = _broadcast_confirmations[confirmation_token]
    decision = _broadcast_limiter.check(admin_id)
    if not decision.allowed:
        return _rate_limited_response(decision)

    user_ids = list(preview_record.get('recipient_ids') or ())
    token = os.getenv('TELEGRAM_BOT_TOKEN', '')
    if not token:
        return _json_response({'detail': 'bot token unavailable'}, status=500)
    if not _consume_broadcast_confirmation(admin_id, text, confirmation_token):
        return _json_response({'detail': 'confirmation already used'}, status=409)

    now_str = datetime.now(KYIV_TZ).strftime('%Y-%m-%d %H:%M:%S')
    broadcast_id = await db.create_broadcast(text, now_str)

    sent_ids, failed_ids, skipped_ids = [], [], []
    receipts = []  # (user_id, status, message_id, reason)
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    async with aiohttp.ClientSession() as session:
        for uid in user_ids:
            # Skip the synthetic QA test accounts (999000***) that never
            # correspond to a real Telegram chat.
            if str(uid).startswith('999000'):
                skipped_ids.append(str(uid))
                receipts.append((uid, 'skipped', None, 'synthetic test id'))
                continue
            try:
                async with session.post(url, json={'chat_id': int(uid), 'text': text}) as resp:
                    payload = {}
                    try:
                        payload = await resp.json()
                    except Exception:
                        pass
                    if resp.status == 200 and payload.get('ok'):
                        mid = (payload.get('result') or {}).get('message_id')
                        sent_ids.append(str(uid))
                        receipts.append((uid, 'sent', mid, None))
                    else:
                        reason = payload.get('description', '') or f'HTTP {resp.status}'
                        failed_ids.append({'id': str(uid), 'reason': reason})
                        receipts.append((uid, 'failed', None, reason))
            except Exception as e:
                failed_ids.append({'id': str(uid), 'reason': str(e)})
                receipts.append((uid, 'failed', None, str(e)))
                logger.warning(f'admin broadcast to {uid} failed: {e}')
            await asyncio.sleep(0.05)  # stay well under Telegram rate limits

    await db.save_broadcast_receipts(
        broadcast_id, receipts, now_str,
        sent=len(sent_ids), failed=len(failed_ids),
        skipped=len(skipped_ids), total=len(user_ids),
    )
    await db.log_admin_action(
        admin_id,
        'broadcast',
        target='all_users',
        metadata={
            'broadcast_id': broadcast_id,
            'text_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'text_length': len(text),
            'sent': len(sent_ids),
            'failed': len(failed_ids),
            'skipped': len(skipped_ids),
        },
    )

    logger.info(f"API admin broadcast #{broadcast_id}: sent={len(sent_ids)} "
                f"failed={len(failed_ids)} skipped={len(skipped_ids)}")
    return _json_response({
        'confirmed': True,
        'broadcast_id': broadcast_id,
        'sent': len(sent_ids), 'failed': len(failed_ids), 'skipped': len(skipped_ids),
        'total_users': len(user_ids),
        'sent_ids': sent_ids, 'failed_ids': failed_ids, 'skipped_ids': skipped_ids,
    })


async def api_admin_broadcasts_list(request: web.Request):
    """GET /api/admin/broadcasts — history of all broadcasts (admin only)."""
    if not is_admin(request['user_id']):
        return _json_response({'detail': 'admin only'}, status=403)
    rows = await db.list_broadcasts(limit=50)
    return _json_response({'broadcasts': rows})


async def api_admin_audit(request: web.Request):
    if not is_admin(request['user_id']):
        return _json_response({'detail': 'admin only'}, status=403)
    return _json_response({'events': await db.list_admin_audit(limit=100)})


async def api_admin_broadcast_detail(request: web.Request):
    """GET /api/admin/broadcasts/{id} — per-recipient receipts with message_id."""
    if not is_admin(request['user_id']):
        return _json_response({'detail': 'admin only'}, status=403)
    try:
        bid = int(request.match_info['id'])
    except (KeyError, ValueError):
        return _json_response({'detail': 'invalid id'}, status=400)
    data = await db.get_broadcast_receipts(bid)
    if not data:
        return _json_response({'detail': 'broadcast not found'}, status=404)
    return _json_response(data)


async def api_admin_users(request: web.Request):
    """GET /api/admin/users — admin-only roster straight from the DB.
    Proves exactly who is registered (id, name, @username, first/last seen)."""
    if not is_admin(request['user_id']):
        return _json_response({'detail': 'admin only'}, status=403)
    users = await db.get_all_users()
    real = [u for u in users if not str(u['user_id']).startswith('999000')]
    test = [u for u in users if str(u['user_id']).startswith('999000')]
    return _json_response({
        'total': len(users),
        'real_count': len(real),
        'test_count': len(test),
        'users': users,
    })


async def api_admin_feedback(request: web.Request):
    """GET /api/admin/feedback — per-feature 👍/👎 tally + who reacted + comments."""
    if not is_admin(request['user_id']):
        return _json_response({'detail': 'admin only'}, status=403)
    return _json_response(await db.get_feedback_summary())


def build_api_app() -> web.Application:
    """Build and return the aiohttp API application."""
    # Order matters: json_errors first (catches everything else), then CORS
    # (so the error JSON also carries CORS headers), then init-data auth.
    app = web.Application(middlewares=[
        json_errors_middleware,
        cors_middleware,
        preauth_rate_limit_middleware,
        init_data_middleware,
        user_rate_limit_middleware,
        user_context_middleware,
    ])
    app.router.add_route('GET', '/api/health', api_health)
    app.router.add_route('GET', '/api/me', api_me)
    app.router.add_route('GET', '/api/exchange-rates', api_exchange_rates)
    app.router.add_route('GET', '/api/balance', api_balance)
    app.router.add_route('GET', '/api/transactions', api_get_transactions)
    app.router.add_route('GET', '/api/quick-templates', api_quick_templates)
    app.router.add_route('POST', '/api/transactions', api_post_transaction)
    app.router.add_route('PATCH', '/api/transactions/{id}', api_patch_transaction)
    app.router.add_route('DELETE', '/api/transactions/{id}', api_delete_transaction)
    app.router.add_route('GET', '/api/reports/monthly', api_monthly_report)
    app.router.add_route(
        'GET', '/api/reports/payment-sources', api_report_payment_sources
    )
    app.router.add_route('GET', '/api/budgets', api_budgets_get)
    app.router.add_route('PUT', '/api/budgets', api_budgets_put)
    app.router.add_route(
        'DELETE', '/api/budgets/{type}/{category}', api_budgets_delete
    )
    app.router.add_route(
        'GET', '/api/recurring-operations', api_recurring_list
    )
    app.router.add_route(
        'POST', '/api/recurring-operations', api_recurring_create
    )
    app.router.add_route(
        'PATCH', '/api/recurring-operations/{id}', api_recurring_patch
    )
    app.router.add_route(
        'DELETE', '/api/recurring-operations/{id}', api_recurring_delete
    )
    app.router.add_route(
        'GET', '/api/recurring-suggestions', api_recurring_suggestions
    )
    app.router.add_route('GET', '/api/insights', api_insights)
    app.router.add_route('GET', '/api/digest/weekly', api_weekly_digest)
    app.router.add_route('GET', '/api/forecast', api_forecast)
    app.router.add_route(
        'GET', '/api/settings/notifications', api_notification_settings
    )
    app.router.add_route(
        'PATCH', '/api/settings/notifications', api_notification_settings_patch
    )
    app.router.add_route('GET', '/api/categories', api_categories)
    app.router.add_route('GET', '/api/settings', api_settings)

    # ---- new parity routes ----
    # reports
    app.router.add_route('GET', '/api/reports/employees', api_report_employees)
    app.router.add_route('GET', '/api/reports/tax', api_report_tax)
    app.router.add_route('GET', '/api/reports/accounting', api_report_accounting)
    app.router.add_route('GET', '/api/reports/time', api_report_time)
    app.router.add_route('GET', '/api/reports/category-breakdown', api_report_category_breakdown)
    # categories CRUD
    app.router.add_route('GET', '/api/categories/full', api_categories_full)
    app.router.add_route('POST', '/api/categories', api_categories_create)
    # subcategories (registered BEFORE the {type}/{name} routes so the longer
    # path matches first)
    app.router.add_route('POST', '/api/categories/{type}/{name}/subcategories', api_subcategories_create)
    app.router.add_route('DELETE', '/api/categories/{type}/{name}/subcategories/{sub}', api_subcategories_delete)
    app.router.add_route('PATCH', '/api/categories/{type}/{name}', api_categories_update)
    app.router.add_route('DELETE', '/api/categories/{type}/{name}', api_categories_delete)
    # employees CRUD
    app.router.add_route('GET', '/api/employees', api_employees_list)
    app.router.add_route('POST', '/api/employees', api_employees_create)
    app.router.add_route('DELETE', '/api/employees/{name}', api_employees_delete)
    # time categories CRUD
    app.router.add_route('GET', '/api/time-categories', api_time_categories_list)
    app.router.add_route('POST', '/api/time-categories', api_time_categories_create)
    app.router.add_route('DELETE', '/api/time-categories/{name}', api_time_categories_delete)
    # time tracks
    app.router.add_route('GET', '/api/time-tracks', api_time_tracks_list)
    app.router.add_route('POST', '/api/time-tracks', api_time_tracks_create)
    app.router.add_route('DELETE', '/api/time-tracks/{id}', api_time_tracks_delete)
    # tax settings
    app.router.add_route('PATCH', '/api/settings/tax', api_settings_tax_update)
    app.router.add_route('DELETE', '/api/settings', api_settings_reset)
    app.router.add_route('DELETE', '/api/account', api_account_delete)
    # admin
    app.router.add_route('POST', '/api/admin/broadcast', api_admin_broadcast)
    app.router.add_route('GET', '/api/admin/broadcasts', api_admin_broadcasts_list)
    app.router.add_route('GET', '/api/admin/broadcasts/{id}', api_admin_broadcast_detail)
    app.router.add_route('GET', '/api/admin/audit', api_admin_audit)
    app.router.add_route('GET', '/api/admin/users', api_admin_users)
    app.router.add_route('GET', '/api/admin/feedback', api_admin_feedback)

    # Catch-all OPTIONS for CORS preflight on any path
    async def options_handler(_request):
        return web.Response(status=204)

    app.router.add_route('OPTIONS', '/{path_info:.*}', options_handler)
    return app


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log Telegram failures and give users a stable, non-sensitive response."""
    error = getattr(context, 'error', None)
    logger.exception(
        "Unhandled Telegram update error (%s)",
        type(error).__name__ if error is not None else 'unknown',
        exc_info=(type(error), error, error.__traceback__) if error is not None else None,
    )
    message = (
        getattr(update, 'effective_message', None)
        or getattr(update, 'message', None)
        or getattr(getattr(update, 'callback_query', None), 'message', None)
    )
    if message is None or not hasattr(message, 'reply_text'):
        return
    try:
        await message.reply_text(
            "⚠️ Сталася тимчасова помилка. Спробуйте ще раз трохи пізніше."
        )
    except Exception:
        logger.warning("Could not send generic Telegram error response")


def main():
    """Start the bot"""
    import datetime as _dt
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is required')

    application = Application.builder().token(TOKEN).post_init(post_init_notify).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("info", show_info))
    application.add_handler(CommandHandler("settings", show_settings))
    application.add_handler(CommandHandler("privacy", privacy_command))
    application.add_handler(CommandHandler("terms", terms_command))
    application.add_handler(CommandHandler("clear", clear_account_command))
    application.add_handler(CommandHandler("admin_stats", admin_stats))
    application.add_handler(CommandHandler("stats", admin_stats))  # short alias
    application.add_handler(CommandHandler("admin", admin_stats))  # shorter alias
    application.add_handler(CommandHandler("list_users", admin_list_users))
    application.add_handler(CommandHandler("users", admin_list_users))
    application.add_handler(CommandHandler("cleanup_users", admin_cleanup_users))
    application.add_handler(CommandHandler("cleanup", admin_cleanup_users))
    application.add_handler(CommandHandler("reset_user_settings", admin_reset_user_settings))
    application.add_handler(CommandHandler("reset", admin_reset_user_settings))
    application.add_handler(CommandHandler("broadcast", admin_broadcast))

    # Callbacks
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Text messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))
    application.add_error_handler(error_handler)

    # Only the primary worker may register jobs. Redirect/migration services
    # set ENABLE_SCHEDULED_JOBS=false to avoid duplicate writes/messages.
    if application.job_queue and ENABLE_SCHEDULED_JOBS:
        application.job_queue.run_once(
            daily_backup_job,
            when=10,
            name='startup_backup',
        )
        application.job_queue.run_daily(
            daily_backup_job,
            time=_dt.time(hour=3, minute=0, tzinfo=KYIV_TZ),
            name='daily_backup'
        )
        application.job_queue.run_daily(
            recurring_operations_job,
            time=_dt.time(hour=4, minute=0, tzinfo=KYIV_TZ),
            name='recurring_operations',
        )
        application.job_queue.run_daily(
            weekly_digest_job,
            time=_dt.time(hour=19, minute=0, tzinfo=KYIV_TZ),
            name='weekly_digest',
        )
        logger.info(
            "Scheduled jobs enabled: backup, recurring operations, weekly digest"
        )
    elif not ENABLE_SCHEDULED_JOBS:
        logger.info("Scheduled jobs disabled by ENABLE_SCHEDULED_JOBS")

    logger.info("Bot %s started", _bot_handle())
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
