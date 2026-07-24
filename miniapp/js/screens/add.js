/* Add screen — three modes: Витрата / Дохід / Час */

import { Store, navigate } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { toast, esc, setHTML } from '../ui.js';
import {
  ensureClientRequestId,
  friendlySubmitError,
  normalizeQuickTemplates,
  templateToDraft,
} from '../add-flow.js';
import { paymentSourceLabel, paymentSourceOptions } from '../block2-ui.js';

const state = {
  mode: 'expense',   // expense | income | time
  amount: '0',
  currency: 'UAH',
  paymentSource: null,
  category: null,
  subcategory: null,  // optional, only when the chosen category has subcategories
  note: '',
  counterparty: '',
  empOpen: false,  // employees-submenu collapse state
  submitting: false,
  submitError: '',
  submitSuccess: '',
  clientRequestId: null,
  quick: {
    status: 'idle', // idle | loading | ready | error
    templates: [],
    repeatLast: null,
    error: '',
  },
};

function resetDraftFields(nextMode = 'expense') {
  state.mode = nextMode;
  state.amount = '0';
  state.currency = 'UAH';
  state.paymentSource = null;
  state.category = null;
  state.subcategory = null;
  state.note = '';
  state.counterparty = '';
  state.empOpen = false;
}

function clearDraft(nextMode = 'expense') {
  resetDraftFields(nextMode);
  state.submitting = false;
  state.submitError = '';
  state.submitSuccess = '';
  state.clientRequestId = null;
}

function clearSubmitFeedback() {
  state.submitError = '';
  state.submitSuccess = '';
}

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

function templateAmountLabel(item) {
  const amount = Number(item.amount).toLocaleString('uk-UA', { maximumFractionDigits: 2 });
  return `${amount} ${item.currency}`;
}

function quickTemplateButton(item, attrs, { repeat = false } = {}) {
  const title = repeat ? 'Повторити останню' : (item.label || item.category);
  const hierarchy = item.subcategory ? `${item.category} · ${item.subcategory}` : item.category;
  return `
    <button class="quick-template-card ${repeat ? 'repeat' : ''}" type="button" ${attrs}>
      <span class="quick-template-icon" aria-hidden="true">${repeat ? '↻' : (item.type === 'income' ? '+' : '−')}</span>
      <span class="quick-template-copy">
        <strong>${esc(title)}</strong>
        <span>${esc(hierarchy)} · ${esc(templateAmountLabel(item))} · ${esc(paymentSourceLabel(item.paymentSource))}</span>
      </span>
    </button>`;
}

function quickTemplatesMarkup() {
  const { status, templates, repeatLast, error } = state.quick;
  if (status === 'loading' || status === 'idle') {
    return `
      <div class="quick-template-panel" aria-busy="true">
        <div class="quick-template-head"><strong>Швидке додавання</strong></div>
        <div class="quick-template-loading"><span class="add-spinner" aria-hidden="true"></span> Завантажуємо шаблони…</div>
      </div>`;
  }
  if (status === 'error') {
    return `
      <div class="quick-template-panel">
        <div class="quick-template-head"><strong>Швидке додавання</strong></div>
        <div class="quick-template-error" role="status">${esc(error || 'Не вдалося завантажити шаблони.')}</div>
        <button class="quick-template-retry" type="button" data-quick-retry>Спробувати ще раз</button>
      </div>`;
  }

  const cards = [
    repeatLast ? quickTemplateButton(repeatLast, 'data-repeat-last', { repeat: true }) : '',
    ...templates.slice(0, 4).map((item, index) =>
      quickTemplateButton(item, `data-template-index="${index}"`)),
  ].filter(Boolean).join('');

  return `
    <div class="quick-template-panel">
      <div class="quick-template-head">
        <strong>Швидке додавання</strong>
        <span>заповнює форму, не зберігає автоматично</span>
      </div>
      ${cards
        ? `<div class="quick-template-strip">${cards}</div>`
        : '<div class="quick-template-empty">Шаблони з’являться після кількох операцій.</div>'}
    </div>`;
}

function submitDockMarkup(isTime) {
  const statusText = state.submitError || state.submitSuccess || (state.submitting ? 'Зберігаємо…' : '');
  const statusKind = state.submitError ? 'error' : (state.submitSuccess ? 'success' : '');
  const buttonText = state.submitSuccess
    ? 'Збережено'
    : state.submitting
      ? 'Зберігаємо…'
      : state.submitError
        ? 'Спробувати ще раз'
        : isTime
          ? 'Зберегти запис часу'
          : 'Зберегти операцію';
  return `
    <div class="add-submit-dock">
      <div class="add-submit-status ${statusKind}" id="addSubmitStatus" role="status" aria-live="polite">${esc(statusText)}</div>
      <button class="btn btn-primary add-save-btn" id="saveBtn" aria-busy="${state.submitting ? 'true' : 'false'}" ${state.submitting || state.submitSuccess ? 'disabled' : ''}>
        ${state.submitting ? '<span class="add-spinner" aria-hidden="true"></span>' : ''}
        <span>${esc(buttonText)}</span>
      </button>
    </div>`;
}

function template() {
  const isTime = state.mode === 'time';
  const display = state.amount === '0' ? '0' : state.amount;
  const cats = categoriesFor(state.mode);

  return `
    <div id="quickTemplatesSlot">${quickTemplatesMarkup()}</div>
    <div class="add-editor">
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

    ${!isTime ? `
      <div class="section-head payment-source-head">
        <div class="section-title">Джерело коштів</div>
      </div>
      <div class="payment-source-grid" role="group" aria-label="Джерело коштів">
        <button
          type="button"
          class="payment-source-chip ${state.paymentSource === null ? 'active' : ''}"
          data-payment-source=""
          aria-pressed="${state.paymentSource === null}"
        ><span aria-hidden="true">∅</span>Не вказано</button>
        ${paymentSourceOptions().map(({ value, label, icon }) => `
          <button
            type="button"
            class="payment-source-chip ${state.paymentSource === value ? 'active' : ''}"
            data-payment-source="${value}"
            aria-pressed="${state.paymentSource === value}"
          ><span aria-hidden="true">${esc(icon)}</span>${esc(label)}</button>
        `).join('')}
      </div>
      ${state.paymentSource === null ? '<p class="payment-source-hint">Оберіть джерело для точнішого звіту або залиште «Не вказано».</p>' : ''}
    ` : ''}

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

    ${(() => {
      // Subcategory chips — only when a category is chosen AND it has subcategories
      if (isTime || !state.category) return '';
      const subs = Store.subcategoriesFor(state.mode, state.category);
      if (!subs.length) return '';
      return `
        <div class="section-head" style="margin-top: var(--sp-3);">
          <div class="section-title">Підрозділ <span class="sub-optional">(необов'язково)</span></div>
        </div>
        <div class="chip-grid">
          ${subs.map((s) =>
            `<button class="chip sub-chip ${state.subcategory === s ? 'active' : ''}" data-sub="${esc(s)}">${esc(s)}</button>`
          ).join('')}
        </div>`;
    })()}

    ${isTime ? '' : `
    <div class="field" style="margin-top: var(--sp-4);">
      <label>${state.mode === 'income' ? 'Від кого (необов\'язково)' : 'Кому (необов\'язково)'}</label>
      <input class="input" id="counterpartyInput" placeholder="${state.mode === 'income' ? 'напр. Іваненко О. / ТОВ «Ромашка»' : 'напр. орендодавцю, постачальнику'}" value="${esc(state.counterparty)}">
    </div>`}

    <div class="field" style="margin-top: var(--sp-4);">
      <label>${isTime ? 'Опис (необов\'язково)' : 'Коментар (необов\'язково)'}</label>
      <input class="input" id="noteInput" placeholder="${isTime ? 'напр. підготовка позову' : 'напр. кава з клієнтом'}" value="${esc(state.note)}">
    </div>

    </div>
    ${submitDockMarkup(isTime)}
  `;
}

function rerender() {
  renderAdd({ preserve: true });
}

function syncSubmitDock(root) {
  const status = root.querySelector('#addSubmitStatus');
  const button = root.querySelector('#saveBtn');
  const message = state.submitError || state.submitSuccess || (state.submitting ? 'Зберігаємо…' : '');
  if (status) {
    status.textContent = message;
    status.className = `add-submit-status ${state.submitError ? 'error' : (state.submitSuccess ? 'success' : '')}`;
  }
  if (button) {
    const buttonText = state.submitSuccess
      ? 'Збережено'
      : state.submitting
        ? 'Зберігаємо…'
        : state.submitError
          ? 'Спробувати ще раз'
          : state.mode === 'time'
            ? 'Зберегти запис часу'
            : 'Зберегти операцію';
    button.disabled = state.submitting || Boolean(state.submitSuccess);
    button.setAttribute('aria-busy', state.submitting ? 'true' : 'false');
    setHTML(button, `${state.submitting ? '<span class="add-spinner" aria-hidden="true"></span>' : ''}<span>${esc(buttonText)}</span>`);
  }
  root.querySelectorAll('.add-editor button, .add-editor input').forEach((control) => {
    control.disabled = state.submitting;
  });
}

function bindQuickTemplateActions(root) {
  root.querySelector('[data-quick-retry]')?.addEventListener('click', () => {
    loadQuickTemplates(root, { force: true });
  });
  root.querySelector('[data-repeat-last]')?.addEventListener('click', () => {
    applyQuickTemplate(state.quick.repeatLast);
  });
  root.querySelectorAll('[data-template-index]').forEach((button) => {
    button.addEventListener('click', () => {
      applyQuickTemplate(state.quick.templates[Number(button.dataset.templateIndex)]);
    });
  });
}

function renderQuickTemplatesSlot(root) {
  const slot = root.querySelector('#quickTemplatesSlot');
  if (!slot) return;
  setHTML(slot, quickTemplatesMarkup());
  bindQuickTemplateActions(root);
}

async function loadQuickTemplates(root, { force = false } = {}) {
  if (!force && ['loading', 'ready'].includes(state.quick.status)) return;
  state.quick.status = 'loading';
  state.quick.error = '';
  renderQuickTemplatesSlot(root);
  try {
    const normalized = normalizeQuickTemplates(await Api.quickTemplates());
    state.quick.templates = normalized.templates;
    state.quick.repeatLast = normalized.repeatLast;
    state.quick.status = 'ready';
  } catch (error) {
    state.quick.status = 'error';
    state.quick.error = error?.message || 'Не вдалося завантажити шаблони.';
  }
  renderQuickTemplatesSlot(root);
}

function applyQuickTemplate(item) {
  if (!item || state.submitting) return;
  const availableCategories = categoriesFor(item.type);
  const availableSubcategories = Store.subcategoriesFor(item.type, item.category);
  const draft = templateToDraft(item, {
    categories: availableCategories,
    subcategories: availableSubcategories,
  });
  if (!draft) {
    Telegram.haptic('error');
    toast('Цей шаблон більше не доступний');
    return;
  }

  state.mode = draft.mode;
  state.amount = draft.amount;
  state.currency = draft.currency;
  state.paymentSource = draft.paymentSource;
  state.category = draft.category;
  state.subcategory = draft.subcategory;
  state.note = draft.note;
  state.counterparty = '';  // a repeated template is a new entry — re-enter who
  state.empOpen = Boolean(draft.category?.startsWith(_empPrefix(draft.mode)));
  clearSubmitFeedback();
  Telegram.haptic('selection');
  toast(draft.category
    ? 'Шаблон заповнено — перевірте та збережіть'
    : 'Суму заповнено — оберіть актуальну категорію');
  rerender();
}

function showSubmitError(root, message) {
  state.submitting = false;
  state.submitSuccess = '';
  state.submitError = message;
  syncSubmitDock(root);
  Telegram.haptic('error');
  toast(message);
}

async function submitAdd(root) {
  if (state.submitting || state.submitSuccess) return;

  let response;
  const completedMode = state.mode;
  if (state.mode === 'time') {
    const minutes = Number.parseInt(state.amount, 10);
    if (!minutes || minutes <= 0) {
      showSubmitError(root, 'Введіть тривалість у хвилинах');
      return;
    }
    if (!state.category) {
      showSubmitError(root, 'Оберіть активність');
      return;
    }
    state.clientRequestId = ensureClientRequestId(state.clientRequestId);
    state.submitting = true;
    clearSubmitFeedback();
    syncSubmitDock(root);
    try {
      response = await Api.addTimeTrack({
        minutes,
        category: state.category,
        description: state.note || state.category,
        client_request_id: state.clientRequestId,
      });
    } catch (error) {
      showSubmitError(root, friendlySubmitError(error, 'Не вдалося зберегти запис часу'));
      return;
    }
  } else {
    const amount = Number.parseFloat(state.amount);
    if (!amount || amount <= 0) {
      showSubmitError(root, 'Введіть суму');
      return;
    }
    if (!state.category) {
      showSubmitError(root, 'Оберіть категорію');
      return;
    }
    state.clientRequestId = ensureClientRequestId(state.clientRequestId);
    state.submitting = true;
    clearSubmitFeedback();
    syncSubmitDock(root);
    try {
      response = await Api.addTransaction({
        type: state.mode,
        amount,
        currency: state.currency,
        category: state.category,
        subcategory: state.subcategory || undefined,
        counterparty: state.counterparty || undefined,
        description: state.note || state.category,
        payment_source: state.paymentSource,
        client_request_id: state.clientRequestId,
      });
    } catch (error) {
      // Retain clientRequestId: a retry is safe even if the first response was lost.
      showSubmitError(root, friendlySubmitError(error, 'Не вдалося зберегти операцію'));
      return;
    }
  }

  const duplicate = Boolean(response?.duplicate);
  state.submitSuccess = duplicate ? 'Операцію вже було збережено' : 'Успішно збережено';
  state.submitError = '';
  state.submitting = true;
  resetDraftFields(completedMode);
  state.clientRequestId = null;
  state.quick.status = 'idle';
  syncSubmitDock(root);
  Telegram.haptic('success');
  toast(duplicate
    ? 'Операцію вже було збережено'
    : completedMode === 'time' ? 'Записано' : 'Операцію збережено');

  try {
    await Store.hydrate();
  } catch {
    toast('Дані збережено. Огляд оновиться при наступному відкритті.');
  }
  clearDraft(completedMode);
  navigate('home');
}

function bind(root) {
  bindQuickTemplateActions(root);
  root.querySelectorAll('[data-mode]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    state.mode = button.dataset.mode;
    state.category = null;
    state.subcategory = null;
    state.amount = '0';
    state.paymentSource = null;
    clearSubmitFeedback();
    Telegram.haptic('selection');
    rerender();
  }));
  root.querySelectorAll('[data-cur]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    state.currency = button.dataset.cur;
    clearSubmitFeedback();
    Telegram.haptic('selection');
    rerender();
  }));
  root.querySelectorAll('[data-payment-source]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    state.paymentSource = button.dataset.paymentSource || null;
    clearSubmitFeedback();
    Telegram.haptic('selection');
    rerender();
  }));
  root.querySelectorAll('[data-quick]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    state.amount = button.dataset.quick;
    clearSubmitFeedback();
    Telegram.haptic('light');
    rerender();
  }));
  root.querySelectorAll('[data-cat]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    state.category = button.dataset.cat;
    state.subcategory = null;
    clearSubmitFeedback();
    Telegram.haptic('selection');
    rerender();
  }));
  root.querySelectorAll('[data-sub]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    state.subcategory = state.subcategory === button.dataset.sub ? null : button.dataset.sub;
    clearSubmitFeedback();
    Telegram.haptic('selection');
    rerender();
  }));
  root.querySelector('#empToggle')?.addEventListener('click', () => {
    if (state.submitting) return;
    state.empOpen = !state.empOpen;
    Telegram.haptic('selection');
    rerender();
  });
  root.querySelectorAll('[data-key]').forEach((button) => button.addEventListener('click', () => {
    if (state.submitting) return;
    const key = button.dataset.key;
    Telegram.haptic('light');
    clearSubmitFeedback();
    if (key === '⌫') {
      state.amount = state.amount.length > 1 ? state.amount.slice(0, -1) : '0';
    } else if (key === '.') {
      if (state.mode !== 'time' && !state.amount.includes('.')) state.amount += '.';
    } else {
      state.amount = state.amount === '0' ? key : state.amount + key;
    }
    const display = root.querySelector('#amountDisplay');
    if (display) {
      display.textContent = state.amount;
      const currency = document.createElement('span');
      currency.className = 'currency';
      currency.textContent = state.mode === 'time' ? 'хв' : symbolFor(state.currency);
      display.appendChild(currency);
      display.classList.toggle('dim', state.amount === '0');
    }
    syncSubmitDock(root);
  }));
  root.querySelector('#counterpartyInput')?.addEventListener('input', (event) => {
    state.counterparty = event.target.value;
    clearSubmitFeedback();
    syncSubmitDock(root);
  });
  root.querySelector('#noteInput')?.addEventListener('input', (event) => {
    state.note = event.target.value;
    clearSubmitFeedback();
    syncSubmitDock(root);
  });
  root.querySelector('#saveBtn')?.addEventListener('click', () => submitAdd(root));
  syncSubmitDock(root);
}

export function renderAdd(opts = {}) {
  if (opts.kind && ['income', 'expense', 'time'].includes(opts.kind)) {
    clearDraft(opts.kind);
    if (opts.kind !== 'time' && paymentSourceOptions().some(({ value }) => value === opts.paymentSource)) {
      state.paymentSource = opts.paymentSource;
    }
  }
  const root = document.getElementById('screen-add');
  if (!root) return;
  setHTML(root, template());
  bind(root);
  if (state.quick.status === 'idle') loadQuickTemplates(root);
}
