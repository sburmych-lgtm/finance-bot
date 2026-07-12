# Ruby Finance

Telegram-бот і Mini App для персонального та ФОП-обліку: доходи, витрати, час, мультивалюта, категорії, підкатегорії та звіти. Дані і налаштування ізольовані за Telegram user ID.

## Структура

- `bot.py` — Telegram bot, SQLite і JSON API на `aiohttp`.
- `miniapp/` — статичний Mini App без build-кроку.
- `test_bot.py`, `test_api_integrity.py`, `miniapp/test_server.py` — Python-тести.
- `miniapp/js/*.test.mjs` — Node.js regression-тести.

## Змінні середовища

Скопіюйте `.env.example` у `.env`. Реальні секрети не можна комітити або передавати в чатах.

| Змінна | Сервіс | Призначення |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | worker | Токен від BotFather. Обов’язковий. |
| `ADMIN_IDS` | worker | Telegram ID адмінів через кому. |
| `DATA_DIR` | worker | Каталог постійного сховища; на Railway — mount Volume. |
| `DB_FILE` | worker | Необов’язковий override шляху SQLite. |
| `SETTINGS_FILE` | worker | Необов’язковий override шляху settings JSON. |
| `API_BASE_URL` | Mini App | HTTPS URL worker-сервісу без кінцевого `/`. |
| `BUILD_ID` | Mini App | Необов’язковий cache-busting ID. |
| `PORT` | обидва | Локальний override; Railway встановлює автоматично. |

## Локальний запуск

```powershell
Copy-Item .env.example .env
# Заповніть TELEGRAM_BOT_TOKEN та ADMIN_IDS у .env
python -m pip install -r requirements.txt
python -m dotenv run -- python bot.py
```

Mini App запускається окремо; `API_BASE_URL` має вказувати на worker:

```powershell
$env:PORT = '8081'
python -m dotenv run -- python miniapp/server.py
```

## Перевірки

```powershell
python -m pip install pytest
python -m pytest -q
python -c "import bot"
python -m compileall -q bot.py miniapp
Get-ChildItem miniapp/js -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName }
node --test miniapp/js/*.test.mjs
```

CI виконує ці перевірки на push у `main` і для pull request, а також сканує зміни на секрети.

## Railway

- `worker`: `python bot.py`, Volume mounted at `DATA_DIR`.
- `finance-bot`: `python miniapp/server.py`, `API_BASE_URL` points to the worker public URL.
- Deployment model: push to GitHub `main` → Railway auto-deploy.

Токени ротуються через BotFather і оновлюються лише в Railway Variables та локальному `.env`, ніколи в коді.
