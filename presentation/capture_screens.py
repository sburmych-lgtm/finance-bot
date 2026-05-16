"""Capture Ruby Finance Mini App screenshots for the pitch deck.

Run separately, after starting the preview server on http://localhost:5500.

Output: presentation/screenshots/{home,add,reports,history,settings}.png
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'screenshots'
OUT.mkdir(exist_ok=True)


MOCK_DATA = """
(() => {
  if (!window.Ruby?.Store) return 'no Store';

  const transactions = [
    { id: 1, type: 'income',  category: 'Консультації', amount: 50,    currency: 'USD', amount_uah: 32000, description: 'Консультація клієнта', date: '2026-05-16', timestamp: '2026-05-16 14:30:00' },
    { id: 2, type: 'income',  category: 'ВЛК',          amount: 28000, currency: 'UAH', amount_uah: 28000, description: 'ВЛК справи',           date: '2026-05-14', timestamp: '2026-05-14 10:00:00' },
    { id: 3, type: 'income',  category: 'Суди',         amount: 24000, currency: 'UAH', amount_uah: 24000, description: 'судові справи',        date: '2026-05-12', timestamp: '2026-05-12 10:00:00' },
    { id: 4, type: 'income',  category: 'Фріланс',      amount: 15000, currency: 'UAH', amount_uah: 15000, description: 'консультації онлайн',  date: '2026-05-08', timestamp: '2026-05-08 10:00:00' },
    { id: 5, type: 'income',  category: 'Від Катя',     amount: 18000, currency: 'UAH', amount_uah: 18000, description: 'клієнтська робота',    date: '2026-05-15', timestamp: '2026-05-15 09:00:00' },
    { id: 6, type: 'income',  category: 'Від Ілона',    amount: 22000, currency: 'UAH', amount_uah: 22000, description: 'клієнтська робота',    date: '2026-05-13', timestamp: '2026-05-13 09:00:00' },
    { id: 7, type: 'expense', category: 'Податки',      amount: 12000, currency: 'UAH', amount_uah: 12000, description: 'ЄП + ЄСВ',             date: '2026-05-10', timestamp: '2026-05-10 12:00:00' },
    { id: 8, type: 'expense', category: 'Продукти',     amount: 9800,  currency: 'UAH', amount_uah: 9800,  description: 'Сільпо',               date: '2026-05-09', timestamp: '2026-05-09 18:45:00' },
    { id: 9, type: 'expense', category: 'Транспорт',    amount: 5200,  currency: 'UAH', amount_uah: 5200,  description: 'таксі і бензин',       date: '2026-05-08', timestamp: '2026-05-08 09:20:00' },
    { id: 10,type: 'expense', category: 'ЗП Катя',      amount: 12000, currency: 'UAH', amount_uah: 12000, description: 'зарплата',             date: '2026-05-01', timestamp: '2026-05-01 09:00:00' },
    { id: 11,type: 'expense', category: 'ЗП Ілона',     amount: 14000, currency: 'UAH', amount_uah: 14000, description: 'зарплата',             date: '2026-05-01', timestamp: '2026-05-01 09:00:00' }
  ];
  window.Ruby.Store.balance = { income: 139000, expense: 53000, balance: 86000, currency: 'UAH' };
  window.Ruby.Store.transactions = transactions;
  window.Ruby.Store.rates = { USD: 41.5, EUR: 45.2 };
  window.Ruby.Store.categories = {
    expense: ['Продукти','Кафе','Транспорт','Розваги',"Здоров'я",'Подарунки','Податки','Косметолог','Салон краси','Одяг','Комунальні','ЗП Катя','ЗП Ілона','ЗП Мирослав','Інше'],
    income:  ['Зарплата','Фріланс','Консультації','ВЛК','ТЦК','Суди','Від Катя','Від Ілона','Від Мирослав','Інше']
  };
  window.Ruby.Store.timeCategories = {
    'Сон':       {emoji:'😴'},  'Робота':    {emoji:'💼'},
    'Зал':       {emoji:'🏋️'},  'Їжа':       {emoji:'🍽️'},
    'Терапія':   {emoji:'🧘'},  'Навчання':  {emoji:'🎓'},
    'Стосунки':  {emoji:'💕'},  'Розваги':   {emoji:'🎉'},
    'Скрол стрічки': {emoji:'📱'}, 'Інше':   {emoji:'📦'}
  };
  window.Ruby.Store.employees = ['Катя','Ілона','Мирослав','Христина','Інші'];

  // Mock fetch — intercept /api/* and reply with deterministic data
  const realFetch = window.fetch;
  window.fetch = async (url, opts) => {
    const u = (typeof url === 'string') ? url : url.url || '';
    if (!u.includes('/api/')) return realFetch(url, opts);
    const route = u.split('?')[0];
    const reply = (body, status=200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: {'Content-Type':'application/json'} }));
    if (route.endsWith('/api/reports/employees')) {
      return reply([
        { name: 'Катя',     income: 18000, salary: 12000, profit:  6000, roi:  50.0 },
        { name: 'Ілона',    income: 22000, salary: 14000, profit:  8000, roi:  57.1 },
        { name: 'Мирослав', income: 15000, salary: 16000, profit: -1000, roi:  -6.3 },
        { name: 'Христина', income:  8000, salary:  6000, profit:  2000, roi:  33.3 },
      ]);
    }
    if (route.endsWith('/api/reports/tax')) {
      return reply({
        year: 2026, month: 5, month_name: 'Травень',
        total_income: 139000, total_expense: 53000, profit: 86000,
        single_tax_rate: 0.05, esv_fixed: 1760,
        single_tax: 6950, total_tax: 8710, after_tax: 77290,
        period_from: '2026-05-01', period_to: '2026-05-31'
      });
    }
    if (route.endsWith('/api/reports/accounting')) {
      return reply({
        total_income: 139000, total_expense: 53000, profit: 86000,
        opening_balance: 248000, closing_balance: 334000,
        entries: [
          { debit:'Дт 301', credit:'Кт 701', amount: 139000, label: 'Виручка' },
          { debit:'Дт 901', credit:'Кт 301', amount:  53000, label: 'Витрати' }
        ],
        result: 'profit'
      });
    }
    if (route.endsWith('/api/reports/time')) {
      return reply({
        total_minutes: 9420, total_hours: 157.0, days_in_month: 31, avg_per_day_hours: 5.1,
        by_category: [
          { name:'Робота',    emoji:'💼',  minutes: 4800, hours: 80.0, percentage: 51.0 },
          { name:'Сон',       emoji:'😴',  minutes: 2400, hours: 40.0, percentage: 25.5 },
          { name:'Зал',       emoji:'🏋️', minutes:  720, hours: 12.0, percentage:  7.6 },
          { name:'Навчання',  emoji:'🎓',  minutes:  600, hours: 10.0, percentage:  6.4 },
          { name:'Терапія',   emoji:'🧘',  minutes:  300, hours:  5.0, percentage:  3.2 },
          { name:'Скрол стрічки', emoji:'📱', minutes: 360, hours: 6.0, percentage: 3.8 },
          { name:'Інше',      emoji:'📦',  minutes:  240, hours:  4.0, percentage:  2.5 }
        ],
        productive_minutes: 6120, unproductive_minutes: 600, rest_minutes: 2400, untracked_minutes: 35220
      });
    }
    if (route.endsWith('/api/categories/full')) {
      return reply({
        expense: {
          'Продукти': {emoji:'🛒', keywords:['продукти','магазин','сільпо','атб']},
          'Кафе':     {emoji:'☕', keywords:['кава','кафе','ресторан','обід']},
          'Транспорт':{emoji:'🚕', keywords:['таксі','uber','bolt','метро']},
          'Розваги':  {emoji:'🎭', keywords:['кіно','клуб','концерт']},
          "Здоров'я": {emoji:'💊', keywords:['аптека','лікар','pharmacy']},
          'Подарунки':{emoji:'🎁', keywords:['подарунки','gift']},
          'Податки':  {emoji:'📋', keywords:['податки','пдв','єдиний податок']},
          'Косметолог':{emoji:'💄', keywords:['косметолог']},
          'Салон краси':{emoji:'💅', keywords:['салон','перукар','манікюр']},
          'Одяг':     {emoji:'👗', keywords:['одяг','взуття']},
          'Комунальні':{emoji:'🏠', keywords:['комунальні','світло','газ','опалення']},
          'Інше':     {emoji:'📦', keywords:[]}
        },
        income: {
          'Зарплата':    {emoji:'💰', keywords:['зарплата','зп']},
          'Фріланс':     {emoji:'💼', keywords:['фріланс','проект']},
          'Консультації':{emoji:'⚖️', keywords:['консультація']},
          'ВЛК':         {emoji:'🏥', keywords:['влк']},
          'Суди':        {emoji:'🏛️', keywords:['суд']},
          'Інше':        {emoji:'📦', keywords:[]}
        }
      });
    }
    if (route.endsWith('/api/employees')) {
      return reply(['Катя','Ілона','Мирослав','Христина','Інші']);
    }
    if (route.endsWith('/api/time-categories')) {
      return reply(window.Ruby.Store.timeCategories);
    }
    if (route.endsWith('/api/settings')) {
      return reply({
        employees: ['Катя','Ілона','Мирослав','Христина','Інші'],
        tax_config: { single_tax_rate: 0.05, esv_fixed: 1760, note: 'Для ФОП 3 група' }
      });
    }
    return reply({ detail: 'mocked: no handler' }, 404);
  };

  return 'ok';
})()
"""


SCREENS = ['home', 'add', 'reports', 'history', 'settings']
EXTRA_AI_SHOT = True  # also produce reports_ai.png with the AI modal open

# Additional shots for new feature parity (Reports tabs + Settings CRUD + Time mode)
EXTRA_TABS = [
    # (filename, jsCallback returning Promise when DOM is ready)
    ('reports_employees',  'document.querySelector(\'[data-tab="employees"]\').click()'),
    ('reports_tax',        'document.querySelector(\'[data-tab="tax"]\').click()'),
    ('reports_accounting', 'document.querySelector(\'[data-tab="accounting"]\').click()'),
    ('reports_time',       'document.querySelector(\'[data-tab="time"]\').click()'),
]
EXTRA_SETTINGS = [
    ('settings_categories', '[data-go="expense_cats"]'),
    ('settings_employees',  '[data-go="employees"]'),
    ('settings_tax',        '[data-go="tax"]'),
]
EXTRA_ADD_TIME = True  # capture add screen with mode=time


async def run():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.stderr.write("Install: pip install playwright && playwright install chromium\n")
        sys.exit(1)

    url = 'http://localhost:5500'
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={'width': 390, 'height': 844},
            device_scale_factor=2,
            color_scheme='dark',
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until='networkidle')
        await page.wait_for_timeout(800)
        # inject mock store data
        await page.evaluate(MOCK_DATA)
        await page.wait_for_timeout(200)

        for name in SCREENS:
            await page.click(f'[data-nav="{name}"]')
            await page.wait_for_timeout(400)
            out = OUT / f'{name}.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'  captured {out}')

        if EXTRA_AI_SHOT:
            # Reports → AI tab → click button → screenshot modal
            await page.click('[data-nav="reports"]')
            await page.wait_for_timeout(400)
            await page.evaluate('document.querySelector(\'[data-tab="ai"]\')?.click()')
            await page.wait_for_timeout(300)
            await page.click('#genAIBtn')
            await page.wait_for_timeout(500)
            out = OUT / 'reports_ai.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'  captured {out}')
            # Close the modal so subsequent shots are clean
            await page.evaluate('document.querySelector(".ai-modal-close")?.click()')
            await page.wait_for_timeout(200)

        # Reports tabs: Працівники / Податки / Бухгалтерія / Час
        for name, js in EXTRA_TABS:
            await page.click('[data-nav="reports"]')
            await page.wait_for_timeout(200)
            await page.evaluate(js)
            await page.wait_for_timeout(600)  # API call settles
            out = OUT / f'{name}.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'  captured {out}')

        # Settings sub-screens — reload between to reset section state
        for name, selector in EXTRA_SETTINGS:
            await page.goto(url, wait_until='networkidle')
            await page.wait_for_timeout(400)
            await page.evaluate(MOCK_DATA)
            await page.wait_for_timeout(200)
            await page.click('[data-nav="settings"]')
            await page.wait_for_timeout(400)
            await page.click(selector)
            await page.wait_for_timeout(700)
            out = OUT / f'{name}.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'  captured {out}')

        # Add screen — time mode
        if EXTRA_ADD_TIME:
            await page.click('[data-nav="add"]')
            await page.wait_for_timeout(200)
            await page.evaluate('document.querySelector(\'[data-mode="time"]\')?.click()')
            await page.wait_for_timeout(400)
            out = OUT / 'add_time.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'  captured {out}')

        await browser.close()


if __name__ == '__main__':
    asyncio.run(run())
