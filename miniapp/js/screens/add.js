/* Add screen — three modes: Витрата / Дохід / Час */

import { Store, navigate } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { toast, esc } from '../ui.js';

const state = {
  mode: 'expense',   // expense | income | time
  amount: '0',
  currency: 'UAH',
  category: null,
  note: '',
  empOpen: false,  // employees-submenu collapse state
};

function symbolFor(cur) {
  return cur === 'UAH' ? '₴' : cur === 'USD' ? '$' : cur === 'EUR' ? '€' : esc(cur);
}

// Employee categories follow the bot's naming convention:
//   • income  → 'Від <name>'  (we received money from this person)
//   • expense → 'ЗП <name>'   (we paid salary to this person)
function _empPrefix(mode) {
  return mode === 'income' ? 'Від ' : 'ЗП ';
}

function categoriesFor(mode) {
  const fallbackExpense = ['Продукти', 'Кафе', 'Транспорт', 'Розваги', "Здоров'я", 'Подарунки', 'Податки', 'Одяг', 'Комунальні', 'Інше'];
  const fallbackIncome  = ['Зарплата', 'Фріланс', 'Консультації', 'Інше'];
  const fallbackTime    = ['Сон', 'Робота', 'Зал', 'Їжа', 'Терапія', 'Навчання', 'Скрол стрічки', 'Розваги', 'Інше'];
  if (mode === 'time') {
    const cats = Store.timeCategories;
    if (cats && typeof cats === 'object') return Object.keys(cats);
    return fallbackTime;
  }
  const cats = Store.categories?.[mode];
  if (cats && Array.isArray(cats)) return cats;
  return mode === 'income' ? fallbackIncome : fallbackExpense;
}

function splitCategoriesByEmployee(mode, allCats) {
  const prefix = _empPrefix(mode);
  const regular = allCats.filter((c) => !c.startsWith(prefix));
  const employeeNames = allCats
    .filter((c) => c.startsWith(prefix))
    .map((c) => c.slice(prefix.length));
  return { regular, employeeNames };
}

function emojiFor(mode, cat) {
  if (mode === 'time' && Store.timeCategories?.[cat]?.emoji) return Store.timeCategories[cat].emoji;
  return null;
}

function template() {
  const isTime = state.mode === 'time';
  const display = state.amount === '0' ? '0' : state.amount;
  const cats = categoriesFor(state.mode);

  return `
    <div class="kind-pills" style="grid-template-columns: 1fr 1fr 1fr;">
      <button class="kind-pill expense ${state.mode === 'expense' ? 'active' : ''}" data-mode="expense">− Витрата</button>
      <button class="kind-pill income  ${state.mode === 'income'  ? 'active' : ''}" data-mode="income">+ Дохід</button>
      <button class="kind-pill ${state.mode === 'time'    ? 'active time' : ''}" data-mode="time">⏱ Час</button>
    </div>

    <div class="add-amount-panel">
      <div class="amount-display ${state.amount === '0' ? 'dim' : ''}" id="amountDisplay">
        ${esc(display)}<span class="currency">${isTime ? 'хв' : symbolFor(state.currency)}</span>
      </div>

      ${!isTime ? `
        <div class="segmented" style="margin: var(--sp-2) 0 0;">
          <button class="segment ${state.currency === 'UAH' ? 'active' : ''}" data-cur="UAH">UAH ₴</button>
          <button class="segment ${state.currency === 'USD' ? 'active' : ''}" data-cur="USD">USD $</button>
          <button class="segment ${state.currency === 'EUR' ? 'active' : ''}" data-cur="EUR">EUR €</button>
        </div>
      ` : `
        <div class="segmented" style="margin: var(--sp-2) 0 0;">
          <button class="segment" data-quick="30">30 хв</button>
          <button class="segment" data-quick="60">1 год</button>
          <button class="segment" data-quick="90">1.5 год</button>
          <button class="segment" data-quick="120">2 год</button>
        </div>
      `}

      <div class="numpad">
        ${['1','2','3','4','5','6','7','8','9','.','0','⌫'].map((k) =>
          `<button class="numkey ${k === '⌫' || k === '.' ? 'action' : ''}" data-key="${esc(k)}">${esc(k)}</button>`
        ).join('')}
      </div>
    </div>

    <div class="section-head" style="margin-top: var(--sp-4);">
      <div class="section-title">${isTime ? 'Активність' : 'Категорія'}</div>
    </div>
    ${(() => {
      if (isTime) {
        return `<div class="chip-grid">${cats.map((c) => {
          const em = emojiFor('time', c);
          const label = em ? `${em} ${c}` : c;
          return `<button class="chip ${state.category === c ? 'active' : ''}" data-cat="${esc(c)}">${esc(label)}</button>`;
        }).join('')}</div>`;
      }
      // Money mode: split into regular categories + employees submenu
      const { regular, employeeNames } = splitCategoriesByEmployee(state.mode, cats);
      const prefix = _empPrefix(state.mode);
      const groupLabel = state.mode === 'income' ? '👥 Від працівників' : '💼 ЗП працівникам';
      const empActive = state.category && state.category.startsWith(prefix);
      const regularHtml = `<div class="chip-grid">${regular.map((c) => {
        return `<button class="chip ${state.category === c ? 'active' : ''}" data-cat="${esc(c)}">${esc(c)}</button>`;
      }).join('')}</div>`;
      const empHtml = employeeNames.length ? `
        <button class="emp-group-toggle ${(state.empOpen || empActive) ? 'open' : ''} ${empActive ? 'active' : ''}" id="empToggle">
          <span>${esc(groupLabel)}</span>
          <span class="emp-arrow">${(state.empOpen || empActive) ? '▾' : '▸'}</span>
        </button>
        ${(state.empOpen || empActive) ? `
          <div class="chip-grid emp-grid">
            ${employeeNames.map((n) => {
              const cat = prefix + n;
              return `<button class="chip emp-chip ${state.category === cat ? 'active' : ''}" data-cat="${esc(cat)}">${esc(n)}</button>`;
            }).join('')}
          </div>` : ''}
      ` : '';
      return regularHtml + empHtml;
    })()}

    <div class="field" style="margin-top: var(--sp-4);">
      <label>${isTime ? 'Опис (необов\'язково)' : 'Коментар (необов\'язково)'}</label>
      <input class="input" id="noteInput" placeholder="${isTime ? 'напр. підготовка позову' : 'напр. кава з клієнтом'}" value="${esc(state.note)}">
    </div>

    <button class="btn btn-primary" id="saveBtn" style="margin-top: var(--sp-4);">
      ${isTime ? 'Зберегти запис часу' : 'Зберегти операцію'}
    </button>
  `;
}

function bind(root) {
  root.querySelectorAll('[data-mode]').forEach((b) => b.addEventListener('click', () => {
    state.mode = b.dataset.mode;
    state.category = null;
    state.amount = '0';
    Telegram.haptic('selection');
    renderAdd();
  }));
  root.querySelectorAll('[data-cur]').forEach((b) => b.addEventListener('click', () => {
    state.currency = b.dataset.cur;
    Telegram.haptic('selection');
    renderAdd();
  }));
  root.querySelectorAll('[data-quick]').forEach((b) => b.addEventListener('click', () => {
    state.amount = b.dataset.quick;
    Telegram.haptic('light');
    renderAdd();
  }));
  root.querySelectorAll('[data-cat]').forEach((b) => b.addEventListener('click', () => {
    state.category = b.dataset.cat;
    Telegram.haptic('selection');
    renderAdd();
  }));
  root.querySelector('#empToggle')?.addEventListener('click', () => {
    state.empOpen = !state.empOpen;
    Telegram.haptic('selection');
    renderAdd();
  });
  root.querySelectorAll('[data-key]').forEach((b) => b.addEventListener('click', () => {
    const k = b.dataset.key;
    Telegram.haptic('light');
    if (k === '⌫') {
      state.amount = state.amount.length > 1 ? state.amount.slice(0, -1) : '0';
    } else if (k === '.') {
      if (state.mode !== 'time' && !state.amount.includes('.')) state.amount = state.amount + '.';
    } else {
      state.amount = state.amount === '0' ? k : (state.amount + k);
    }
    const display = document.getElementById('amountDisplay');
    if (display) {
      display.textContent = state.amount;
      const cur = document.createElement('span');
      cur.className = 'currency';
      cur.textContent = state.mode === 'time' ? 'хв' : symbolFor(state.currency);
      display.appendChild(cur);
      display.classList.toggle('dim', state.amount === '0');
    }
  }));
  root.querySelector('#noteInput')?.addEventListener('input', (e) => {
    state.note = e.target.value;
  });
  root.querySelector('#saveBtn').addEventListener('click', async () => {
    if (state.mode === 'time') {
      const minutes = parseInt(state.amount, 10);
      if (!minutes || minutes <= 0) { toast('Введіть тривалість у хвилинах'); return; }
      if (!state.category) { toast('Оберіть активність'); return; }
      try {
        await Api.addTimeTrack({ minutes, category: state.category, description: state.note || state.category });
        Telegram.haptic('success');
        toast('Записано');
        state.amount = '0'; state.category = null; state.note = '';
        await Store.hydrate();
        navigate('home');
      } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
      return;
    }
    const amount = parseFloat(state.amount);
    if (!amount || amount <= 0) { toast('Введіть суму'); return; }
    if (!state.category) { toast('Оберіть категорію'); return; }
    try {
      await Api.addTransaction({
        type: state.mode,
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
    } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
  });
}

export function renderAdd(opts = {}) {
  if (opts.kind && ['income', 'expense', 'time'].includes(opts.kind)) {
    state.mode = opts.kind;
    state.category = null;
    state.amount = '0';
  }
  const root = document.getElementById('screen-add');
  if (!root) return;
  root.innerHTML = template();
  bind(root);
}
