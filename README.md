# Фінансовий бот (@Olesia_money_bot)

Telegram-бот для обліку особистих фінансів українською — записує витрати/доходи, рахує баланс і робить місячні звіти по категоріях.

## Команди
- `/start`, `/help`, `/допомога` — довідка
- `/balance`, `/баланс` — поточний баланс
- `/history`, `/історія` — останні 15 транзакцій
- `/report [місяць]`, `/звіт [місяць]` — звіт за місяць
- `/clear`, `/очистити` — стерти свої дані
- `/myid` — дізнатись свій Telegram ID (потрібно для адміна)
- `/broadcast <текст>` — розіслати повідомлення всім (тільки адмін)

## Запис транзакцій
Просто пишіть боту:
- `100 кава`
- `500 таксі 15.12`
- `зарплата 30000`

## Локальний запуск
```bash
cp .env.example .env
# заповніть BOT_TOKEN (отримати в @BotFather) і ADMIN_ID
pip install -r requirements.txt
python bot.py
```

## Деплой на Railway
1. `railway login`
2. `railway init` (новий проєкт)
3. `railway up` (деплой)
4. У Variables додати `BOT_TOKEN` і `ADMIN_ID`
5. `Procfile` запустить воркер: `worker: python bot.py`

## Тести
```bash
pip install pytest
pytest -q
```
