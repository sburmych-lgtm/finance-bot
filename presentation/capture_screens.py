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
  window.Ruby.Store.balance = { income: 142800, expense: 56380, balance: 86420, currency: 'UAH' };
  window.Ruby.Store.transactions = [
    { id: 1, type: 'income',  category: 'Консультації', amount: 50,    currency: 'USD', amount_uah: 32000, description: 'Консультація клієнта',     date: '2026-05-16', timestamp: '2026-05-16 14:30:00' },
    { id: 2, type: 'income',  category: 'ВЛК',          amount: 28000, currency: 'UAH', amount_uah: 28000, description: 'ВЛК справи',               date: '2026-05-14', timestamp: '2026-05-14 10:00:00' },
    { id: 3, type: 'income',  category: 'Суди',         amount: 24000, currency: 'UAH', amount_uah: 24000, description: 'судові справи',            date: '2026-05-12', timestamp: '2026-05-12 10:00:00' },
    { id: 4, type: 'income',  category: 'Фріланс',      amount: 15000, currency: 'UAH', amount_uah: 15000, description: 'консультації онлайн',      date: '2026-05-08', timestamp: '2026-05-08 10:00:00' },
    { id: 5, type: 'income',  category: 'Зарплата',     amount: 0,     currency: 'UAH', amount_uah: 0,     description: 'нічого',                    date: '2026-05-15', timestamp: '2026-05-15 08:00:00' },
    { id: 6, type: 'expense', category: 'Податки',      amount: 12000, currency: 'UAH', amount_uah: 12000, description: 'ЄП + ЄСВ',                  date: '2026-05-10', timestamp: '2026-05-10 12:00:00' },
    { id: 7, type: 'expense', category: 'Продукти',     amount: 9800,  currency: 'UAH', amount_uah: 9800,  description: 'Сільпо',                    date: '2026-05-09', timestamp: '2026-05-09 18:45:00' },
    { id: 8, type: 'expense', category: 'Транспорт',    amount: 5200,  currency: 'UAH', amount_uah: 5200,  description: 'таксі і бензин',            date: '2026-05-08', timestamp: '2026-05-08 09:20:00' },
    { id: 9, type: 'expense', category: 'Кафе',         amount: 4500,  currency: 'UAH', amount_uah: 4500,  description: 'кафе/ресторани',            date: '2026-05-07', timestamp: '2026-05-07 11:15:00' },
    { id: 10,type: 'expense', category: 'Розваги',      amount: 3200,  currency: 'UAH', amount_uah: 3200,  description: 'кіно і театр',              date: '2026-05-06', timestamp: '2026-05-06 20:00:00' }
  ].filter(t => t.amount_uah > 0);
  window.Ruby.Store.rates = { USD: 41.5, EUR: 45.2 };
  return 'ok';
})()
"""


SCREENS = ['home', 'add', 'reports', 'history', 'settings']
EXTRA_AI_SHOT = True  # also produce reports_ai.png with the AI modal open


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
            # Go to Reports, scroll AI card into view, open modal, capture
            await page.click('[data-nav="reports"]')
            await page.wait_for_timeout(300)
            await page.evaluate("document.querySelector('#aiCard')?.scrollIntoView({block:'center'})")
            await page.wait_for_timeout(200)
            await page.click('#genAIBtn')
            await page.wait_for_timeout(500)
            out = OUT / 'reports_ai.png'
            await page.screenshot(path=str(out), full_page=False)
            print(f'  captured {out}')

        await browser.close()


if __name__ == '__main__':
    asyncio.run(run())
