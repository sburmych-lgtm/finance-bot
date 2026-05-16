/* History screen — full transaction list with per-row delete */

import { Store } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, fmtDate, esc, toast } from '../ui.js';

const CATEGORY_LETTER = {
  'Продукти':'П','Кафе':'К','Транспорт':'Т','Розваги':'Р','Здоров\'я':'Z',
  'Подарунки':'G','Податки':'%','Одяг':'O','Комунальні':'H','Інше':'•',
  'Зарплата':'$','Фріланс':'F','Консультації':'L',
};

export function renderHistory() {
  const root = document.getElementById('screen-history');
  if (!root) return;
  const txs = Store.transactions || [];
  if (!txs.length) {
    root.innerHTML = `
      <div class="empty-state">
        <div class="icon">≡</div>
        <h3>Історія порожня</h3>
        <p>Додайте першу операцію — і вона з'явиться тут.</p>
      </div>`;
    return;
  }

  const byDay = {};
  for (const t of txs) {
    byDay[t.date] = byDay[t.date] || [];
    byDay[t.date].push(t);
  }

  root.innerHTML = Object.entries(byDay).map(([day, items]) => `
    <div class="section-head" style="margin-top: var(--sp-4);">
      <div class="section-title">${esc(fmtDate(day))}</div>
      <div class="section-link" style="cursor: default; pointer-events: none;">${items.length} оп.</div>
    </div>
    <div class="row-list">
      ${items.map((t) => `
        <div class="row history-row" data-tx-id="${esc(String(t.id))}">
          <div class="avatar">${esc(CATEGORY_LETTER[t.category] || (t.category?.[0] || '•').toUpperCase())}</div>
          <div>
            <div class="row-title">${esc(t.category || 'Інше')}</div>
            <div class="row-meta">${esc(String(t.description || '').slice(0, 40))}</div>
          </div>
          <div class="amount ${t.type === 'expense' ? 'expense' : 'income'}">${esc(fmtMoney(
            t.type === 'expense' ? -(t.amount_uah || t.amount) : (t.amount_uah || t.amount),
            'UAH'
          ))}</div>
          <button class="history-del" data-del="${esc(String(t.id))}" aria-label="Видалити">×</button>
        </div>
      `).join('')}
    </div>
  `).join('');

  // Per-row delete handler — same UX as the bot's «↩️ Відмінити» but on any row
  root.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const txId = btn.dataset.del;
      const tx = (Store.transactions || []).find((x) => String(x.id) === txId);
      if (!tx) return;
      const amountStr = fmtMoney(
        tx.type === 'expense' ? -(tx.amount_uah || tx.amount) : (tx.amount_uah || tx.amount),
        'UAH'
      );
      if (!window.confirm(`Видалити «${tx.category} · ${amountStr}»? Цю дію неможливо скасувати.`)) return;
      try {
        await Api.deleteTransaction(tx.id);
        Telegram.haptic('success');
        toast('Видалено');
        await Store.hydrate();
        renderHistory();
      } catch (err) {
        Telegram.haptic('error');
        toast(err.message || 'Помилка');
      }
    });
  });
}
