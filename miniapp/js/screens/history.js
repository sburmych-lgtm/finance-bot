/* History screen — typed + date-ranged filtering with per-row delete */

import { Store } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, fmtDate, esc, toast } from '../ui.js';

const CATEGORY_LETTER = {
  'Продукти':'П','Кафе':'К','Транспорт':'Т','Розваги':'Р','Здоров\'я':'Z',
  'Подарунки':'G','Податки':'%','Одяг':'O','Комунальні':'H','Інше':'•',
  'Зарплата':'$','Фріланс':'F','Консультації':'L',
};

const MONTH_NAMES = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                    'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень'];

// Local state lives between renders so the filter bar remembers choices
const state = {
  type: 'all',           // 'all' | 'income' | 'expense'
  period: 'current_month', // 'current_month' | '10d' | '30d' | 'month'
  year: new Date().getFullYear(),
  month: new Date().getMonth() + 1,
  rows: null,             // null = loading, [] = empty, [..] = loaded
  err: null,
};

function letter(cat) {
  return CATEGORY_LETTER[cat] || (cat?.[0] || '•').toUpperCase();
}

function buildQuery() {
  const params = new URLSearchParams();
  params.set('limit', 'all');
  if (state.type !== 'all') params.set('type', state.type);
  if (state.period === 'month') {
    params.set('period', 'month');
    params.set('year', String(state.year));
    params.set('month', String(state.month));
  } else {
    params.set('period', state.period);
  }
  return `?${params.toString()}`;
}

async function fetchRows() {
  state.rows = null;
  state.err = null;
  doRender();
  try {
    const url = `/api/transactions${buildQuery()}`;
    const data = await Api._request(url);
    state.rows = data || [];
  } catch (e) {
    state.err = e.message || 'Не вдалось завантажити';
    state.rows = [];
  }
  doRender();
}

function filterBar() {
  // Type segmented
  const typeSeg = ['all', 'income', 'expense'].map((t) => {
    const label = t === 'all' ? 'Усі' : t === 'income' ? '+ Доходи' : '− Витрати';
    return `<button class="seg-btn ${state.type === t ? 'active' : ''}" data-type="${t}">${label}</button>`;
  }).join('');

  // Period segmented
  const periodOpts = [
    { id: 'current_month', label: 'Цей місяць' },
    { id: '10d',           label: '10 днів' },
    { id: '30d',           label: '30 днів' },
    { id: 'month',         label: 'Конкретний' },
  ];
  const periodSeg = periodOpts.map((p) =>
    `<button class="seg-btn ${state.period === p.id ? 'active' : ''}" data-period="${p.id}">${esc(p.label)}</button>`
  ).join('');

  // Month picker (only when period == 'month')
  let monthPicker = '';
  if (state.period === 'month') {
    const monthOpts = MONTH_NAMES.slice(1).map((m, i) =>
      `<option value="${i + 1}" ${state.month === i + 1 ? 'selected' : ''}>${esc(m)}</option>`
    ).join('');
    const currentYear = new Date().getFullYear();
    const yearOpts = [];
    for (let y = currentYear; y >= currentYear - 5; y--) {
      yearOpts.push(`<option value="${y}" ${state.year === y ? 'selected' : ''}>${y}</option>`);
    }
    monthPicker = `
      <div class="month-year-picker">
        <select class="input" id="hYearSel">${yearOpts.join('')}</select>
        <select class="input" id="hMonthSel">${monthOpts}</select>
      </div>`;
  }

  return `
    <div class="history-filter">
      <div class="segmented type">${typeSeg}</div>
      <div class="segmented period">${periodSeg}</div>
      ${monthPicker}
    </div>`;
}

function summary() {
  if (!state.rows || !state.rows.length) return '';
  let income = 0, expense = 0;
  for (const r of state.rows) {
    const v = r.amount_uah || r.amount || 0;
    if (r.type === 'income') income += v;
    else expense += v;
  }
  const net = income - expense;
  return `
    <div class="balance-card" style="min-height: auto; margin-top: var(--sp-3);">
      <div class="balance-label">${esc(periodLabel())}</div>
      <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtMoney(net, 'UAH'))}</div>
      <div class="metric-row">
        <div class="metric"><span>Доходи</span><strong>${esc(fmtMoney(income, 'UAH'))}</strong></div>
        <div class="metric"><span>Витрати</span><strong>${esc(fmtMoney(expense, 'UAH'))}</strong></div>
      </div>
    </div>`;
}

function periodLabel() {
  if (state.period === 'current_month') return 'Цей місяць';
  if (state.period === '10d') return 'Останні 10 днів';
  if (state.period === '30d') return 'Останні 30 днів';
  return `${MONTH_NAMES[state.month]} ${state.year}`;
}

function rowsList() {
  if (state.rows === null) {
    return `<div class="panel" style="padding: var(--sp-4); margin-top: var(--sp-3);">
      <div class="sk" style="height:48px; margin-bottom:8px;"></div>
      <div class="sk" style="height:48px; margin-bottom:8px;"></div>
      <div class="sk" style="height:48px;"></div>
    </div>`;
  }
  if (state.err) {
    return `<div class="empty-state"><div class="icon">!</div><h3>Помилка</h3><p>${esc(state.err)}</p></div>`;
  }
  if (!state.rows.length) {
    return `<div class="empty-state">
      <div class="icon">≡</div>
      <h3>Нічого не знайдено</h3>
      <p>За цей фільтр немає жодної операції.</p>
    </div>`;
  }
  // Group by day
  const byDay = {};
  for (const t of state.rows) {
    byDay[t.date] = byDay[t.date] || [];
    byDay[t.date].push(t);
  }
  return Object.entries(byDay).map(([day, items]) => `
    <div class="section-head" style="margin-top: var(--sp-4);">
      <div class="section-title">${esc(fmtDate(day))}</div>
      <div class="section-link" style="cursor: default; pointer-events: none;">${items.length} оп.</div>
    </div>
    <div class="row-list">
      ${items.map((t) => `
        <div class="row history-row" data-tx-id="${esc(String(t.id))}">
          <div class="avatar">${esc(letter(t.category))}</div>
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
}

function doRender() {
  const root = document.getElementById('screen-history');
  if (!root) return;
  root.innerHTML = filterBar() + summary() + rowsList();

  // Type buttons
  root.querySelectorAll('[data-type]').forEach((b) => b.addEventListener('click', () => {
    state.type = b.dataset.type;
    Telegram.haptic('selection');
    fetchRows();
  }));
  // Period buttons
  root.querySelectorAll('[data-period]').forEach((b) => b.addEventListener('click', () => {
    state.period = b.dataset.period;
    Telegram.haptic('selection');
    if (state.period !== 'month') {
      fetchRows();
    } else {
      // just re-render the filter bar to expose month selector; fetch happens on change
      doRender();
    }
  }));
  // Month + year selects
  root.querySelector('#hYearSel')?.addEventListener('change', (e) => {
    state.year = parseInt(e.target.value, 10);
    fetchRows();
  });
  root.querySelector('#hMonthSel')?.addEventListener('change', (e) => {
    state.month = parseInt(e.target.value, 10);
    fetchRows();
  });

  // Per-row delete
  root.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const txId = btn.dataset.del;
      const tx = (state.rows || []).find((x) => String(x.id) === txId);
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
        await fetchRows();
      } catch (err) {
        Telegram.haptic('error');
        toast(err.message || 'Помилка');
      }
    });
  });
}

export function renderHistory() {
  // First render: trigger fetch and show skeleton; subsequent filter changes
  // will reach fetchRows() and re-render.
  fetchRows();
}
