"""Generate Ruby Finance pitch deck PDF.

Output: Ruby_Finance_Pitch_Deck.pdf (landscape A4)
"""
from __future__ import annotations
import os
from pathlib import Path

from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.colors import HexColor, Color
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SHOTS = HERE / 'screenshots'
OUT = ROOT / 'Ruby_Finance_Pitch_Deck.pdf'

# ── Brand palette ────────────────────────────────────────────────
INK       = HexColor('#0A0608')
GRAPHITE  = HexColor('#14101A')
FOG       = HexColor('#1E1820')
OXBLOOD   = HexColor('#3A0B14')
BURGUNDY  = HexColor('#6E0F1F')
CRIMSON   = HexColor('#9B1B30')
GOLD      = HexColor('#D8B56D')
GOLD_SOFT = HexColor('#F2E3BE')
IVORY     = HexColor('#F7F1E7')
MUTED     = HexColor('#B8A99D')
DIM       = HexColor('#7A6E66')
SUCCESS   = HexColor('#6FB67E')
DANGER    = HexColor('#D45A4F')


def rgba(hex_color: str, alpha: float) -> Color:
    c = HexColor(hex_color)
    return Color(c.red, c.green, c.blue, alpha=alpha)


# ── Font registration ────────────────────────────────────────────
def _try_register_fonts() -> tuple[str, str, str]:
    """Return tuple (display, body, body_bold). Falls back to Times/Helvetica."""
    candidates = [
        # Windows system fonts
        (r'C:\Windows\Fonts\georgiab.ttf', 'Display'),
        (r'C:\Windows\Fonts\segoeui.ttf',  'Body'),
        (r'C:\Windows\Fonts\segoeuib.ttf', 'BodyBold'),
    ]
    available: dict[str, bool] = {}
    for path, name in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                available[name] = True
            except Exception:
                available[name] = False
    return (
        'Display'  if available.get('Display')  else 'Times-Bold',
        'Body'     if available.get('Body')     else 'Helvetica',
        'BodyBold' if available.get('BodyBold') else 'Helvetica-Bold',
    )


F_DISPLAY, F_BODY, F_BOLD = _try_register_fonts()


# ── Page / layout helpers ────────────────────────────────────────
PAGE_W, PAGE_H = landscape(A4)  # 842x595 pt
MARGIN = 24 * mm


def page_bg(c: Canvas):
    c.setFillColor(INK)
    c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)
    # ambient glow — crimson top-left
    c.saveState()
    for r in range(48, 0, -4):
        alpha = (50 - r) / 800.0
        col = Color(CRIMSON.red, CRIMSON.green, CRIMSON.blue, alpha=alpha)
        c.setFillColor(col)
        c.circle(40 * mm, PAGE_H - 30 * mm, r * mm, fill=1, stroke=0)
    # gold corner top-right
    for r in range(36, 0, -3):
        alpha = (40 - r) / 1200.0
        col = Color(GOLD.red, GOLD.green, GOLD.blue, alpha=alpha)
        c.setFillColor(col)
        c.circle(PAGE_W - 35 * mm, PAGE_H - 25 * mm, r * mm, fill=1, stroke=0)
    # oxblood glow bottom-center
    for r in range(50, 0, -4):
        alpha = (54 - r) / 700.0
        col = Color(BURGUNDY.red, BURGUNDY.green, BURGUNDY.blue, alpha=alpha)
        c.setFillColor(col)
        c.circle(PAGE_W / 2, 0, r * mm, fill=1, stroke=0)
    c.restoreState()


def header_brand(c: Canvas, page_num: int, total: int):
    # Monogram R + wordmark + page indicator
    c.saveState()
    cx, cy = 20 * mm, PAGE_H - 17 * mm
    # monogram tile
    c.setFillColor(GRAPHITE)
    c.setStrokeColor(rgba('#D8B56D', 0.35))
    c.setLineWidth(0.6)
    c.roundRect(cx - 5 * mm, cy - 5 * mm, 10 * mm, 10 * mm, 2 * mm, fill=1, stroke=1)
    c.setFillColor(GOLD)
    c.setFont(F_DISPLAY, 14)
    c.drawCentredString(cx, cy - 2.0, 'R')

    # eyebrow + wordmark
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 7)
    c.drawString(cx + 8 * mm, cy + 1.6 * mm, 'RUBY FINANCE')
    c.setFillColor(MUTED)
    c.setFont(F_BODY, 6.5)
    c.drawString(cx + 8 * mm, cy - 2.4 * mm, 'Telegram Mini App for Professionals')

    # page indicator
    c.setFillColor(DIM)
    c.setFont(F_BODY, 7.5)
    c.drawRightString(PAGE_W - 18 * mm, cy + 1 * mm, f'{page_num:02d}  /  {total:02d}')

    # thin gold hairline
    c.setStrokeColor(rgba('#D8B56D', 0.18))
    c.setLineWidth(0.4)
    c.line(MARGIN, cy - 9 * mm, PAGE_W - MARGIN, cy - 9 * mm)
    c.restoreState()


def footer(c: Canvas):
    c.saveState()
    c.setFillColor(DIM)
    c.setFont(F_BODY, 6.5)
    c.drawString(MARGIN, 8 * mm, '© 2026 Ruby Finance · @Olesia_money_bot · sburmych-lgtm/finance-bot')
    c.drawRightString(PAGE_W - MARGIN, 8 * mm, 'made with Python · aiohttp · SQLite · Railway')
    c.restoreState()


def chrome(c: Canvas, page_num: int, total: int):
    page_bg(c)
    header_brand(c, page_num, total)
    footer(c)


# ── Helpers ──────────────────────────────────────────────────────
def title(c: Canvas, eyebrow: str, heading: str, y: float = PAGE_H - 50 * mm,
          max_w: float | None = None, size: float = 36):
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 9)
    c.drawString(MARGIN, y + 16, eyebrow.upper())
    c.setFillColor(IVORY)
    # auto-shrink to fit max_w (used on screen slides to avoid the phone frame)
    if max_w is not None:
        while size > 14 and c.stringWidth(heading, F_DISPLAY, size) > max_w:
            size -= 1
    c.setFont(F_DISPLAY, size)
    c.drawString(MARGIN, y - 18, heading)


def card(c: Canvas, x, y, w, h, fill=None, stroke_alpha=0.18, radius=4 * mm):
    c.setFillColor(fill or rgba('#FFFFFF', 0.04))
    c.setStrokeColor(rgba('#D8B56D', stroke_alpha))
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, radius, fill=1, stroke=1)


def wrap_lines(c: Canvas, text: str, font: str, size: float, max_w: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ''
    for w in words:
        candidate = f'{cur} {w}'.strip()
        if c.stringWidth(candidate, font, size) <= max_w:
            cur = candidate
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def paragraph(c: Canvas, x, y, w, text, font=F_BODY, size=11, color=MUTED, leading=1.45):
    c.setFillColor(color)
    c.setFont(font, size)
    lines = wrap_lines(c, text, font, size, w)
    for ln in lines:
        c.drawString(x, y, ln)
        y -= size * leading
    return y


def draw_star(c: Canvas, cx, cy, r, fill):
    """Five-pointed star (Telegram Stars badge stand-in)."""
    import math
    c.saveState()
    c.setFillColor(fill)
    p = c.beginPath()
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.42
        x = cx + rad * math.cos(angle)
        y = cy + rad * math.sin(angle)
        if i == 0:
            p.moveTo(x, y)
        else:
            p.lineTo(x, y)
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    c.restoreState()


def bullet(c: Canvas, x, y, w, label, body):
    # gold dot + label + body
    c.setFillColor(GOLD)
    c.circle(x + 1.6 * mm, y + 2 * mm, 1.4, fill=1, stroke=0)
    c.setFillColor(IVORY)
    c.setFont(F_BOLD, 12)
    c.drawString(x + 6 * mm, y, label)
    y -= 6 * mm
    y = paragraph(c, x + 6 * mm, y, w - 6 * mm, body, size=10, color=MUTED)
    return y - 3 * mm


# ── Slides ───────────────────────────────────────────────────────
def slide_cover(c: Canvas, n: int, total: int):
    chrome(c, n, total)
    cy = PAGE_H / 2

    # big monogram
    c.saveState()
    c.setFillColor(GRAPHITE)
    c.setStrokeColor(rgba('#D8B56D', 0.34))
    c.setLineWidth(1.2)
    c.roundRect(MARGIN, cy - 18 * mm, 36 * mm, 36 * mm, 6 * mm, fill=1, stroke=1)
    c.setFillColor(GOLD)
    c.setFont(F_DISPLAY, 64)
    c.drawCentredString(MARGIN + 18 * mm, cy - 6 * mm, 'R')
    c.restoreState()

    # title block
    x = MARGIN + 50 * mm
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 11)
    c.drawString(x, cy + 26 * mm, 'PITCH DECK · MAY 2026')

    c.setFillColor(IVORY)
    c.setFont(F_DISPLAY, 56)
    c.drawString(x, cy + 4 * mm, 'Ruby Finance')

    c.setFillColor(MUTED)
    c.setFont(F_BODY, 14)
    c.drawString(x, cy - 12 * mm, 'Premium finance tracker for lawyers,')
    c.drawString(x, cy - 19 * mm, 'consultants & ФОП — built natively into Telegram.')

    # pill tags
    tags = [('Telegram Mini App', GOLD), ('SQLite + Railway Volume', IVORY), ('Privacy first', SUCCESS)]
    tx = x
    for label, color in tags:
        c.setFillColor(rgba('#FFFFFF', 0.05))
        c.setStrokeColor(rgba('#D8B56D', 0.22))
        c.setLineWidth(0.5)
        w = c.stringWidth(label, F_BOLD, 9) + 8 * mm
        c.roundRect(tx, cy - 36 * mm, w, 6.5 * mm, 3 * mm, fill=1, stroke=1)
        c.setFillColor(color)
        c.setFont(F_BOLD, 9)
        c.drawString(tx + 4 * mm, cy - 33.5 * mm, label)
        tx += w + 3 * mm


def slide_problem(c, n, total):
    chrome(c, n, total)
    title(c, '01 · Проблема', 'Юристам бракує власної аналітики', y=PAGE_H - 40 * mm)

    x = MARGIN
    y = PAGE_H - 80 * mm
    col_w = (PAGE_W - 2 * MARGIN - 2 * 10 * mm) / 3
    items = [
        ('Excel + ChatGPT — не звіт', 'Юрист веде доходи у Google Sheets, копіює у ChatGPT, не отримує системних інсайтів. Час витрачається на ручну роботу замість роботи з клієнтами.'),
        ('ФОП 3 група — спеціфіка', 'Єдиний податок 5% і ЄСВ 1760 ₴ потребують щомісячного контролю. Загальні фінтех-додатки цього не вміють.'),
        ('Працівники як ROI-об\'єкт', 'Якщо у вас 2-5 співробітників — їх ефективність невидима. Хто приносить більше, ніж коштує? Ні Excel, ні CRM не дають відповіді.')
    ]
    for i, (label, body) in enumerate(items):
        cx = x + i * (col_w + 10 * mm)
        card(c, cx, y - 80, col_w, 90)
        c.setFillColor(CRIMSON)
        c.setFont(F_DISPLAY, 28)
        c.drawString(cx + 8 * mm, y - 28, f'0{i+1}')
        c.setFillColor(IVORY)
        c.setFont(F_BOLD, 13)
        c.drawString(cx + 8 * mm, y - 44, label)
        paragraph(c, cx + 8 * mm, y - 60, col_w - 16 * mm, body, size=9.5, leading=1.4)


def slide_solution(c, n, total):
    chrome(c, n, total)
    title(c, '02 · Рішення', 'Один Telegram-бот. Шість екранів. Усе.', y=PAGE_H - 40 * mm)

    x = MARGIN
    y = PAGE_H - 75 * mm

    # 4 pillars
    col_w = (PAGE_W - 2 * MARGIN - 3 * 8 * mm) / 4
    pillars = [
        ('Швидкий ввід', 'Пишіть текстом «100 кава» або натискайте кнопки. Multi-currency UAH/USD/EUR з курсами НБУ.'),
        ('Smart-категоризація', '15 категорій витрат, 7 доходів — авто-визначаються за ключовими словами. Свої — налаштовуються.'),
        ('Звіти + AI', 'Donut-діаграма витрат, bars доходів, місячний баланс. AI-звіт — готовий текст для ChatGPT/Claude.'),
        ('ROI працівників', 'Дохід / ЗП / прибуток / ROI кожного співробітника — за один тап. Точно знаєш, хто окупає себе.')
    ]
    for i, (label, body) in enumerate(pillars):
        cx = x + i * (col_w + 8 * mm)
        card(c, cx, y - 95, col_w, 105)
        # icon tile
        c.setFillColor(rgba('#9B1B30', 0.6))
        c.setStrokeColor(GOLD)
        c.setLineWidth(0.4)
        c.roundRect(cx + 8 * mm, y - 22, 8 * mm, 8 * mm, 2 * mm, fill=1, stroke=1)
        c.setFillColor(GOLD)
        c.setFont(F_DISPLAY, 12)
        c.drawCentredString(cx + 12 * mm, y - 18, str(i + 1))
        c.setFillColor(IVORY)
        c.setFont(F_BOLD, 12)
        c.drawString(cx + 20 * mm, y - 18, label)
        paragraph(c, cx + 8 * mm, y - 38, col_w - 16 * mm, body, size=9, leading=1.4)


def screen_slide(c, n, total, image_path: Path, eyebrow: str, heading: str,
                 description: str, features: list[tuple[str, str]]):
    chrome(c, n, total)
    # screenshot frame on the right (define first so we can size the title accordingly)
    img_w = 65 * mm
    img_h = img_w * 844 / 390  # phone aspect
    ix = PAGE_W - MARGIN - img_w - 4 * mm
    iy = 25 * mm
    # title can occupy left column only — never bleed into the phone
    title_max_w = ix - MARGIN - 14 * mm
    title(c, eyebrow, heading, y=PAGE_H - 36 * mm, max_w=title_max_w, size=30)
    # phone shell glow
    c.saveState()
    for r in range(30, 0, -3):
        alpha = (30 - r) / 600.0
        col = Color(CRIMSON.red, CRIMSON.green, CRIMSON.blue, alpha=alpha)
        c.setFillColor(col)
        c.circle(ix + img_w / 2, iy + img_h / 2, r * mm, fill=1, stroke=0)
    c.restoreState()
    # phone outer frame (subtle gold)
    c.setFillColor(GRAPHITE)
    c.setStrokeColor(rgba('#D8B56D', 0.4))
    c.setLineWidth(1)
    c.roundRect(ix - 2 * mm, iy - 2 * mm, img_w + 4 * mm, img_h + 4 * mm, 5 * mm, fill=1, stroke=1)
    # the image
    c.drawImage(str(image_path), ix, iy, width=img_w, height=img_h,
                preserveAspectRatio=True, mask='auto')

    # left column: description + features
    txt_x = MARGIN
    txt_w = ix - MARGIN - 14 * mm
    y = PAGE_H - 80 * mm
    y = paragraph(c, txt_x, y, txt_w, description, size=12, color=MUTED, leading=1.5)
    y -= 6 * mm

    for label, body in features:
        y = bullet(c, txt_x, y, txt_w, label, body)


def slide_home(c, n, total):
    screen_slide(c, n, total, SHOTS / 'home.png',
        '03 · Mini App',
        'Огляд — баланс за два дотики',
        'Перший екран показує чистий результат місяця, доходи й витрати, а також 8 останніх операцій з кольоровим маркуванням. Все вміщується на одному кадрі — без скролу.',
        [
            ('Hero-картка балансу', 'Серцевина екрану — балансна картка в оxblood-градієнті з ivory-числом. Сума одразу зрозуміла, навіть мимохідь.'),
            ('Швидкі дії', 'Три кнопки: −Витрата, +Дохід, Звіт за місяць. Найчастіша операція доступна за один тап з будь-якого місця.'),
            ('Журнал останніх', 'Кожна операція — категорія, опис, дата, сума з відповідним кольором (зелений / червоний). Натиск — деталі.')
        ])


def slide_add(c, n, total):
    screen_slide(c, n, total, SHOTS / 'add.png',
        '04 · Mini App',
        'Додати — кастомний numpad',
        'Запис операції зведений до мінімуму взаємодій. Перемикач «Витрата / Дохід», вибір валюти, кастомний numpad, чіпи категорій — і Зберегти. Без зайвих діалогів та модалок.',
        [
            ('Numpad замість клавіатури', 'Цифрова панель з кнопками 12pt у серіф-шрифті — нативне відчуття у Telegram WebView, не блимає системна клавіатура.'),
            ('Валютні чіпи', 'UAH / USD / EUR — миттєвий перемикач. Курси НБУ оновлюються автоматично і кешуються 30 хвилин.'),
            ('Чіпи категорій', '15 категорій витрат, 7 категорій доходу — налаштовуються у меню. Емодзі та ключові слова для авто-розпізнавання.')
        ])


def slide_reports(c, n, total):
    screen_slide(c, n, total, SHOTS / 'reports.png',
        '05 · Mini App',
        'Звіти — структура витрат і доходів',
        'Donut-діаграма показує розподіл витрат по топ-6 категоріях за місяць; bars — доходи за джерелами. Це не «графік заради графіка» — кожен елемент відповідає на бізнес-питання.',
        [
            ('Топ-категорії витрат', 'Donut з відсотками одразу видно — куди йдуть гроші. Робота легенди в champagne-сітці.'),
            ('Доходи по джерелах', 'Прогрес-бари показують вагу кожного джерела. Адвокат бачить, чи диверсифікований дохід.'),
            ('AI-звіт у комплекті', 'Окрема дія — згенерувати готовий текст для ChatGPT/Claude/Gemini. Аналіз буде персоналізованим.')
        ])


def slide_ai_report(c, n, total):
    screen_slide(c, n, total, SHOTS / 'reports_ai.png',
        '06 · MINI APP · AI',
        'AI-аналіз у два дотики',
        'На вкладці «Звіти» — окрема кнопка «Згенерувати AI-аналіз». Вона будує структурований промпт із вашими доходами, витратами, відсотками й контекстом ФОП 3 групи. Скопіюйте текст — і вставте у будь-який AI-чат.',
        [
            ('Готовий до ChatGPT / Claude / Gemini', 'Без проміжних кроків — текст уже містить інструкцію аналітику, дані за місяць і конкретні питання, на які треба відповісти.'),
            ('Дані тільки ваші', 'Промпт генерується на пристрої. Жодний AI-провайдер не отримує дані без вашої явної дії — ви самі вирішуєте куди їх вставляти.'),
            ('Контекст українського ФОП', 'Промпт містить підказку для AI: 5% єдиного податку та фіксований ЄСВ 1 760 ₴ — рекомендації будуть локалізовані.')
        ])


def slide_history(c, n, total):
    screen_slide(c, n, total, SHOTS / 'history.png',
        '07 · Mini App',
        'Історія — група за днями',
        'Всі операції згруповані за датою. Кожна група містить кількість операцій і список з категорією, описом, валютною сумою. Це бухгалтерська виписка, але читається як стрічка.',
        [
            ('Денний хедер', 'Серіф-формат дати з лічильником «N оп.» — швидке навігаційне посилання.'),
            ('Кожна операція в окремому рядку', 'Avatar з ініціалом категорії, тайтл, опис, сума з кольором — типовий fintech-патерн.'),
            ('Готово до експорту', 'У Pro-тарифі — експорт у Excel/CSV для бухгалтера. Підпис кожного запису залишається.')
        ])


def slide_settings(c, n, total):
    screen_slide(c, n, total, SHOTS / 'settings.png',
        '08 · Mini App',
        'Меню — профіль, курси, приватність',
        'Профіль Telegram-користувача, поточні курси USD/EUR за НБУ, список редагованих категорій, налаштування податків (ФОП 3 група), кнопка очищення власних даних.',
        [
            ('Профіль з ID', 'Картка з ініціалом і ім\'ям. Підкреслює: дані прив\'язані саме до цього Telegram-юзера.'),
            ('Курси НБУ live', 'USD і EUR за актуальним курсом — джерело офіційне, кеш 30 хв, fallback на історичні.'),
            ('Privacy controls', 'Чесний блок про те, де зберігаються дані (Railway Volume), та одна кнопка «Очистити мої дані».')
        ])


def slide_architecture(c, n, total):
    chrome(c, n, total)
    title(c, '09 · Архітектура', 'Дві служби, одна БД, нуль зайвого', y=PAGE_H - 40 * mm)

    # Pipeline diagram
    y = PAGE_H - 110 * mm
    x = MARGIN + 10 * mm
    boxes = [
        ('Telegram client', 'iOS · Android\nDesktop · Web', GOLD),
        ('Mini App', 'static aiohttp\nHTML / CSS / JS\nRailway service', IVORY),
        ('Bot + API', 'python-telegram-bot\naiohttp REST\nHMAC-SHA256 initData', CRIMSON),
        ('SQLite + Volume', 'finance.db\nsettings.json\nPersistent storage', GOLD)
    ]
    box_w = (PAGE_W - 2 * MARGIN - 6 * 10 * mm) / 4
    box_h = 45 * mm
    for i, (label, body, accent) in enumerate(boxes):
        bx = x + i * (box_w + 10 * mm)
        card(c, bx, y, box_w, box_h, fill=rgba('#FFFFFF', 0.04), stroke_alpha=0.22, radius=4 * mm)
        c.setFillColor(accent)
        c.setFont(F_BOLD, 11)
        c.drawString(bx + 6 * mm, y + box_h - 12 * mm, label)
        c.setFillColor(MUTED)
        c.setFont(F_BODY, 9)
        for j, line in enumerate(body.split('\n')):
            c.drawString(bx + 6 * mm, y + box_h - 20 * mm - j * 12, line)
        # arrow
        if i < 3:
            ax = bx + box_w + 1 * mm
            ay = y + box_h / 2
            c.setStrokeColor(GOLD)
            c.setLineWidth(1.2)
            c.line(ax, ay, ax + 8 * mm, ay)
            # arrowhead
            c.setFillColor(GOLD)
            c.beginPath()
            p = c.beginPath()
            p.moveTo(ax + 8 * mm, ay)
            p.lineTo(ax + 6 * mm, ay + 1.2 * mm)
            p.lineTo(ax + 6 * mm, ay - 1.2 * mm)
            p.close()
            c.drawPath(p, fill=1, stroke=0)

    # Bottom: security tagline
    sy = 30 * mm
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 11)
    c.drawString(MARGIN, sy + 16, 'БЕЗПЕКА')
    c.setFillColor(IVORY)
    c.setFont(F_BODY, 11)
    c.drawString(MARGIN, sy, 'Кожен запит з Mini App несе initData з підписом. Бекенд перевіряє HMAC-SHA256 з токеном бота —')
    c.drawString(MARGIN, sy - 14, 'ніхто інший не може діяти від імені користувача. Жодних паролів, жодних форм входу.')


def slide_monetization(c, n, total):
    chrome(c, n, total)
    title(c, '10 · Монетизація', 'Три тарифи. Один Telegram-чекаут.', y=PAGE_H - 40 * mm)

    y = PAGE_H - 80 * mm
    col_w = (PAGE_W - 2 * MARGIN - 2 * 10 * mm) / 3
    # price tuples: (number_text, suffix_text)  — star is drawn as a shape between them
    plans = [
        ('FREE', '14-денний trial', ('0',  ''),       'Всі функції на старті', [
            'До 50 операцій / місяць',
            'Базові звіти',
            'Без AI-аналізу',
            'Один користувач'
        ], MUTED),
        ('PRO', 'для соло-юриста', ('600',  '/ міс'), '≈ $7.80 — як YNAB', [
            'Unlimited операцій',
            'AI-звіт + експорт',
            'Multi-currency',
            'ФОП-податки'
        ], GOLD),
        ('BUSINESS', 'для офісу з командою', ('1500', '/ міс'), '≈ $19.50 — як QuickBooks', [
            'Все з Pro',
            'ROI працівників',
            'Multi-user',
            'Пріоритетна підтримка'
        ], CRIMSON)
    ]
    for i, (tier, sub, price, anno, feats, accent) in enumerate(plans):
        bx = MARGIN + i * (col_w + 10 * mm)
        bh = 95 * mm
        # highlight middle tier
        is_hi = (i == 1)
        if is_hi:
            card(c, bx, y - bh, col_w, bh, fill=rgba('#3A0B14', 0.7), stroke_alpha=0.45)
        else:
            card(c, bx, y - bh, col_w, bh)

        c.setFillColor(accent)
        c.setFont(F_BOLD, 11)
        c.drawString(bx + 8 * mm, y - 14, tier)

        c.setFillColor(MUTED)
        c.setFont(F_BODY, 9)
        c.drawString(bx + 8 * mm, y - 24, sub)

        # price: <number>  ★  <suffix>
        num, suffix = price
        c.setFillColor(IVORY)
        c.setFont(F_DISPLAY, 24)
        c.drawString(bx + 8 * mm, y - 48, num)
        num_w = c.stringWidth(num, F_DISPLAY, 24)
        star_cx = bx + 8 * mm + num_w + 6
        draw_star(c, star_cx, y - 42, 5, GOLD)
        if suffix:
            c.setFillColor(IVORY)
            c.setFont(F_DISPLAY, 18)
            c.drawString(star_cx + 7, y - 48, ' ' + suffix)

        c.setFillColor(GOLD)
        c.setFont(F_BODY, 8.5)
        c.drawString(bx + 8 * mm, y - 60, anno)

        fy = y - 70
        for f in feats:
            c.setFillColor(GOLD)
            c.circle(bx + 9 * mm, fy + 2, 1.2, fill=1, stroke=0)
            c.setFillColor(IVORY)
            c.setFont(F_BODY, 9.5)
            c.drawString(bx + 12 * mm, fy, f)
            fy -= 11

    # Yearly tease — split text + inline stars
    sy = 20 * mm
    c.setFillColor(MUTED)
    c.setFont(F_BODY, 9.5)
    parts = [
        'Річний тариф — економія до 33%.   Lifetime: Pro  ',
        '14 000',
        '  · Business  ',
        '30 000',
        '.   Telegram забирає ~30% комісії.'
    ]
    total_w = sum(c.stringWidth(p, F_BODY, 9.5) for p in parts) + 2 * 12  # 2 stars × ~12pt slot
    x = PAGE_W / 2 - total_w / 2
    for idx, p in enumerate(parts):
        c.drawString(x, sy, p)
        x += c.stringWidth(p, F_BODY, 9.5)
        if idx in (1, 3):
            draw_star(c, x + 5, sy + 3, 3.5, GOLD)
            x += 10


def slide_roadmap(c, n, total):
    chrome(c, n, total)
    title(c, '11 · Roadmap', 'Що буде далі', y=PAGE_H - 40 * mm)

    # Timeline
    y = PAGE_H - 110 * mm
    items = [
        ('NOW', 'Зараз працює', [
            'Mini App у production на Railway',
            'Bot + JSON API з HMAC-перевіркою',
            'Daily DB backup о 03:00 Kyiv',
            'Privacy Policy + Volume для збереження даних'
        ]),
        ('Q3 2026', 'Монетизація', [
            'Telegram Stars + paywall',
            '14-денний trial для всіх нових юзерів',
            'Tier Free / Pro / Business',
            'FREE_FOREVER для запрошених'
        ]),
        ('Q4 2026', 'Розширення', [
            'Експорт у Excel / CSV',
            'Чарти у самому Mini App (графіки)',
            'Time tracking UI в Mini App',
            'Підтримка multi-office'
        ]),
        ('2027', 'Платформа', [
            'API публічний для інтеграцій',
            'Інтеграція з 1C / SAF-T',
            'Календар і планування витрат',
            'iOS / Android native wrapper'
        ])
    ]
    col_w = (PAGE_W - 2 * MARGIN - 3 * 6 * mm) / 4
    for i, (when, label, feats) in enumerate(items):
        bx = MARGIN + i * (col_w + 6 * mm)
        accent = GOLD if i == 0 else MUTED
        c.setFillColor(accent)
        c.setFont(F_BOLD, 10)
        c.drawString(bx, y + 14, when)
        c.setFillColor(IVORY)
        c.setFont(F_BOLD, 13)
        c.drawString(bx, y - 2, label)
        # vertical hairline
        c.setStrokeColor(rgba('#D8B56D', 0.18))
        c.setLineWidth(0.4)
        c.line(bx, y - 12, bx, y - 95)

        fy = y - 22
        for f in feats:
            c.setFillColor(IVORY if i == 0 else MUTED)
            c.setFont(F_BODY, 9.5)
            for ln in wrap_lines(c, f, F_BODY, 9.5, col_w - 8 * mm):
                c.drawString(bx + 4 * mm, fy, ln)
                fy -= 12
            fy -= 4


def slide_contact(c, n, total):
    chrome(c, n, total)

    # Headline block (top third) — well-spaced vertically
    y_eyebrow = PAGE_H - 48 * mm
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 11)
    c.drawCentredString(PAGE_W / 2, y_eyebrow, '12 · ВІДКРИЙ ЗАРАЗ')

    y_title = y_eyebrow - 38
    c.setFillColor(IVORY)
    c.setFont(F_DISPLAY, 42)
    c.drawCentredString(PAGE_W / 2, y_title, 'Ruby Finance у Telegram')

    # Instruction line with inline gold star
    y_line = y_title - 32
    c.setFillColor(MUTED)
    c.setFont(F_BODY, 13)
    line = 'Зайди в чат  →  натисни кнопку меню       Ruby Finance  →  Mini App відкриється на весь екран.'
    c.drawCentredString(PAGE_W / 2, y_line, line)
    pre = 'Зайди в чат  →  натисни кнопку меню   '
    full_w = c.stringWidth(line, F_BODY, 13)
    pre_w = c.stringWidth(pre, F_BODY, 13)
    star_x = PAGE_W / 2 - full_w / 2 + pre_w + 6
    draw_star(c, star_x, y_line + 4, 4.5, GOLD)

    # Two big tiles (middle band) — start well below the instruction line
    tile_w = 95 * mm
    tile_h = 38 * mm
    gap = 10 * mm
    ty = y_line - 130

    bx = PAGE_W / 2 - tile_w - gap / 2
    card(c, bx, ty, tile_w, tile_h, fill=rgba('#3A0B14', 0.65), stroke_alpha=0.45)
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 9)
    c.drawString(bx + 8 * mm, ty + tile_h - 10 * mm, 'BOT')
    c.setFillColor(IVORY)
    c.setFont(F_DISPLAY, 22)
    c.drawString(bx + 8 * mm, ty + tile_h - 22 * mm, '@Olesia_money_bot')
    c.setFillColor(MUTED)
    c.setFont(F_BODY, 9.5)
    c.drawString(bx + 8 * mm, ty + 6 * mm, 't.me/Olesia_money_bot')

    cx = PAGE_W / 2 + gap / 2
    card(c, cx, ty, tile_w, tile_h)
    c.setFillColor(GOLD)
    c.setFont(F_BOLD, 9)
    c.drawString(cx + 8 * mm, ty + tile_h - 10 * mm, 'GITHUB')
    c.setFillColor(IVORY)
    c.setFont(F_DISPLAY, 18)
    c.drawString(cx + 8 * mm, ty + tile_h - 22 * mm, 'sburmych-lgtm/finance-bot')
    c.setFillColor(MUTED)
    c.setFont(F_BODY, 9.5)
    c.drawString(cx + 8 * mm, ty + 6 * mm, 'github.com/sburmych-lgtm/finance-bot')

    # Status pills (bottom band, well below the tiles)
    pills = [
        ('Status', 'ACTIVE', SUCCESS),
        ('Hosting', 'Railway', GOLD),
        ('Tests', '13 passed', SUCCESS),
        ('Lines of code', '4 414', IVORY)
    ]
    pill_w = 47 * mm
    pill_h = 14 * mm
    pill_gap = 6 * mm
    total_w = 4 * pill_w + 3 * pill_gap
    sy = ty - 30 * mm
    tx = PAGE_W / 2 - total_w / 2
    for label, value, color in pills:
        c.setFillColor(rgba('#FFFFFF', 0.05))
        c.setStrokeColor(rgba('#D8B56D', 0.22))
        c.roundRect(tx, sy, pill_w, pill_h, 3 * mm, fill=1, stroke=1)
        c.setFillColor(MUTED)
        c.setFont(F_BODY, 8)
        c.drawString(tx + 5 * mm, sy + 9 * mm, label.upper())
        c.setFillColor(color)
        c.setFont(F_BOLD, 13)
        c.drawString(tx + 5 * mm, sy + 3 * mm, value)
        tx += pill_w + pill_gap


# ── Build ────────────────────────────────────────────────────────
def build():
    c = Canvas(str(OUT), pagesize=landscape(A4))
    c.setTitle('Ruby Finance — Pitch Deck')
    c.setAuthor('Ruby Finance')
    c.setSubject('Telegram Mini App for Professionals')

    slides = [
        slide_cover,
        slide_problem,
        slide_solution,
        slide_home,
        slide_add,
        slide_reports,
        slide_ai_report,
        slide_history,
        slide_settings,
        slide_architecture,
        slide_monetization,
        slide_roadmap,
        slide_contact,
    ]
    total = len(slides)
    for i, slide in enumerate(slides, 1):
        slide(c, i, total)
        c.showPage()
    c.save()
    print(f'PDF generated: {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)')


if __name__ == '__main__':
    build()
