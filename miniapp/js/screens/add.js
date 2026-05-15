/* Add transaction screen */

import { Store, navigate } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { toast, esc } from '../ui.js';

const state = {
  kind: 'expense',
  amount: '0',
  currency: 'UAH',
  category: null,
  note: '',
};

function categoriesFor(kind) {
  const fallbackExpense = ['Продукти','Кафе','Транспорт','Розваги','Здоров\'я','Подарунки','Податки','Одяг','Комунальні','Інше'];
  const fallbackIncome  = ['Зарплата','Фріланс','Консультації','Інше'];
  const cats = Store.categories?.[kind];
  if (cats && Array.isArray(cats)) return cats;
  return kind === 'income' ? fallbackIncome : fallbackExpense;
}

function symbolFor(cur) {
  return cur === 'UAH' ? '₴' : cur === 'USD' ? '$' : cur === 'EUR' ? '€' : esc(cur);
}

function template() {
  return `
    <div class="kind-pills">
      <button class="kind-pill expense ${state.kind==='expense'?'active':''}" data-kind="expense">− Витрата</button>
      <button class="kind-pill income  ${state.kind==='income' ?'active':''}" data-kind="income">+ Дохід</button>
    </div>
    <div class="add-amount-panel">
      <div class="amount-display ${state.amount==='0'?'dim':''}" id="amountDisplay">
        ${esc(state.amount)}<span class="currency">${symbolFor(state.currency)}</span>
      </div>
      <div class="segmented" style="margin: var(--sp-2) 0 0;">
        <button class="segment ${state.currency==='UAH'?'active':''}" data-cur="UAH">UAH ₴</button>
        <button class="segment ${state.currency==='USD'?'active':''}" data-cur="USD">USD $</button>
        <button class="segment ${state.currency==='EUR'?'active':''}" data-cur="EUR">EUR €</button>
      </div>
      <div class="numpad">
        ${['1','2','3','4','5','6','7','8','9','.','0','⌫'].map(k =>
          `<button class="numkey ${k==='⌫'||k==='.'?'action':''}" data-key="${esc(k)}">${esc(k)}</button>`
        ).join('')}
      </div>
    </div>

    <div class="section-head" style="margin-top: var(--sp-4);">
      <div class="section-title">Категорія</div>
    </div>
    <div class="chip-grid" id="catChips">
      ${categoriesFor(state.kind).map(c =>
        `<button class="chip ${state.category===c?'active':''}" data-cat="${esc(c)}">${esc(c)}</button>`
      ).join('')}
    </div>

    <div class="field" style="margin-top: var(--sp-4);">
      <label>Коментар (необов'язково)</label>
      <input class="input" id="noteInput" placeholder="напр. кава з клієнтом" value="${esc(state.note)}">
    </div>

    <button class="btn btn-primary" id="saveBtn" style="margin-top: var(--sp-4);">
      Зберегти операцію
    </button>
  `;
}

function bind(root) {
  root.querySelectorAll('[data-kind]').forEach((b) => b.addEventListener('click', () => {
    state.kind = b.dataset.kind;
    state.category = null;
    Telegram.haptic('selection');
    renderAdd();
  }));
  root.querySelectorAll('[data-cur]').forEach((b) => b.addEventListener('click', () => {
    state.currency = b.dataset.cur;
    Telegram.haptic('selection');
    renderAdd();
  }));
  root.querySelectorAll('[data-cat]').forEach((b) => b.addEventListener('click', () => {
    state.category = b.dataset.cat;
    Telegram.haptic('selection');
    renderAdd();
  }));
  root.querySelectorAll('[data-key]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.key;
    Telegram.haptic('light');
    if (k === '⌫') {
      state.amount = state.amount.length > 1 ? state.amount.slice(0, -1) : '0';
    } else if (k === '.') {
      if (!state.amount.includes('.')) state.amount = state.amount + '.';
    } else {
      state.amount = state.amount === '0' ? k : (state.amount + k);
    }
    const display = document.getElementById('amountDisplay');
    if (display) {
      // textContent for the digits, separate span for currency to avoid HTML injection.
      display.textContent = state.amount;
      const cur = document.createElement('span');
      cur.className = 'currency';
      cur.textContent = symbolFor(state.currency);
      display.appendChild(cur);
      display.classList.toggle('dim', state.amount === '0');
    }
  }));
  root.querySelector('#noteInput')?.addEventListener('input', (e) => {
    state.note = e.target.value;
  });
  root.querySelector('#saveBtn').addEventListener('click', async () => {
    const amount = parseFloat(state.amount);
    if (!amount || amount <= 0) { toast('Введіть суму'); return; }
    if (!state.category) { toast('Оберіть категорію'); return; }
    try {
      await Api.addTransaction({
        type: state.kind,
        amount,
        currency: state.currency,
        category: state.category,
        description: state.note || state.category,
      });
      Telegram.haptic('success');
      toast('Операцію збережено');
      state.amount = '0'; state.category = null; state.note = '';
      await Store.hydrate();
      navigate('home');
    } catch (e) {
      Telegram.haptic('error');
      toast(e.message || 'Помилка збереження');
    }
  });
}

export function renderAdd(opts = {}) {
  if (opts.kind && (opts.kind === 'income' || opts.kind === 'expense')) {
    state.kind = opts.kind;
    state.category = null;
  }
  const root = document.getElementById('screen-add');
  if (!root) return;
  root.innerHTML = template();
  bind(root);
}
