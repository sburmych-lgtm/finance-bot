/* Home / Огляд */

import { Store } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, fmtDate, esc, toast } from '../ui.js';

const CATEGORY_LETTER = {
  'Продукти':'П','Кафе':'К','Транспорт':'Т','Розваги':'Р','Здоров\'я':'Z',
  'Ліки':'L','Подарунки':'G','Податки':'%','Косметолог':'C','Салон краси':'S',
  'Догляд/Косметика':'D','Вітаміни':'V','Одяг':'O','Комунальні':'H','Інше':'•',
  'Зарплата':'$','Фріланс':'F','Консультації':'L','ВЛК':'M','ТЦК':'M','Суди':'J',
};

function letter(cat) { return CATEGORY_LETTER[cat] || (cat?.[0] || '•').toUpperCase(); }

export function renderHome() {
  const card = document.querySelector('#screen-home .balance-card');
  if (!card) return;

  const b = Store.balance;
  if (b) {
    const month = new Date().toLocaleDateString('uk-UA', { month: 'long', year: 'numeric' });
    card.querySelector('.balance-value').textContent = fmtMoney(b.balance || 0, 'UAH');
    card.querySelector('.balance-value').classList.remove('sk');
    card.querySelector('.balance-sub').textContent = `${month} · чистий результат`;
    card.querySelector('.balance-sub').classList.remove('sk');
    const metrics = card.querySelectorAll('.metric strong');
    metrics[0].textContent = fmtMoney(b.income || 0, 'UAH');
    metrics[1].textContent = fmtMoney(b.expense || 0, 'UAH');
    metrics.forEach((m) => m.classList.remove('sk'));
  }

  // Inject «Відмінити останню» quick-action card if there's something to undo
  const txs = (Store.transactions || []).slice(0, 8);
  injectUndoCard(txs[0]);

  const list = document.getElementById('recent-list');
  if (!list) return;
  if (!txs.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="icon">∅</div>
        <h3>Поки що порожньо</h3>
        <p>Додайте першу операцію через кнопку «+ Додати» — або введіть текстом у чаті бота.</p>
      </div>`;
    return;
  }

  list.innerHTML = txs.map((t) => `
    <div class="row">
      <div class="avatar">${esc(letter(t.category))}</div>
      <div>
        <div class="row-title">${esc(t.category || 'Інше')}</div>
        <div class="row-meta">${esc(fmtDate(t.date))} · ${esc(String(t.description || '').slice(0, 32))}</div>
      </div>
      <div class="amount ${t.type === 'expense' ? 'expense' : 'income'}">${esc(fmtMoney(
        t.type === 'expense' ? -(t.amount_uah || t.amount) : (t.amount_uah || t.amount),
        'UAH'
      ))}</div>
    </div>
  `).join('');
}


function injectUndoCard(lastTx) {
  // Place a discreet «Відмінити останню» button right above the recent-list
  // section so users always know the safety net is there. Mirrors the bot's
  // ↩️ Відмінити останню inline button.
  const existing = document.getElementById('undo-card');
  if (existing) existing.remove();

  if (!lastTx) return;
  const sectionHead = document.querySelector('#screen-home .section-head');
  if (!sectionHead) return;

  const card = document.createElement('div');
  card.id = 'undo-card';
  card.className = 'panel undo-card';
  const isExp = lastTx.type === 'expense';
  const amountStr = fmtMoney(
    isExp ? -(lastTx.amount_uah || lastTx.amount) : (lastTx.amount_uah || lastTx.amount),
    'UAH'
  );
  card.innerHTML = `
    <div class="undo-row">
      <div class="undo-icon">↩</div>
      <div class="undo-body">
        <div class="undo-title">Відмінити останню</div>
        <div class="undo-meta">${esc(lastTx.category || 'Інше')} · ${esc(amountStr)} · ${esc(fmtDate(lastTx.date))}</div>
      </div>
      <button class="undo-btn" id="undoBtn">Видалити</button>
    </div>`;
  sectionHead.parentNode.insertBefore(card, sectionHead);

  document.getElementById('undoBtn').addEventListener('click', async () => {
    const ok = window.confirm(`Видалити операцію «${lastTx.category} · ${amountStr}»? Цю дію неможливо скасувати.`);
    if (!ok) return;
    try {
      await Api.deleteTransaction(lastTx.id);
      Telegram.haptic('success');
      toast('Операцію відмінено');
      await Store.hydrate();
      renderHome();
    } catch (e) {
      Telegram.haptic('error');
      toast(e.message || 'Помилка');
    }
  });
}
