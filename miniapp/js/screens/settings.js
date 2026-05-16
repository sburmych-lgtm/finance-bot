/* Settings screen — full CRUD: categories, employees, time categories, tax, danger zone */

import { Store, navigate } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { toast, esc } from '../ui.js';

const state = {
  section: 'main',  // main | expense_cats | income_cats | time_cats | employees | tax | privacy
  loading: false,
};

function backHeader(label) {
  return `<div class="settings-back" id="settingsBack">
    <button class="ghost-btn" aria-label="Назад">‹</button>
    <div class="settings-back-title">${esc(label)}</div>
  </div>`;
}

function wireBack(root) {
  root.querySelector('#settingsBack')?.addEventListener('click', () => {
    state.section = 'main';
    Telegram.haptic('selection');
    renderSettings();
  });
}

// ── Main settings menu ─────────────────────────────────────────
function renderMain(root) {
  const user = Telegram.user;
  const firstName = String(user?.first_name || 'Користувач');
  const lastName  = String(user?.last_name || '');
  const initial   = (firstName[0] || 'R').toUpperCase();
  const rates = Store.rates || { USD: 41.5, EUR: 45.2 };

  root.innerHTML = `
    <div class="panel" style="padding: var(--sp-4);">
      <div class="brand" style="gap: var(--sp-4);">
        <div class="monogram">${esc(initial)}</div>
        <div class="wordmark">
          <div class="eyebrow">Профіль</div>
          <div class="screen-title" style="font-size: var(--fs-17); margin-top: 0;">${esc(firstName)} ${esc(lastName)}</div>
        </div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Категорії</div></div>
      <div class="row-list">
        <div class="row" data-go="expense_cats"><div class="avatar">−</div>
          <div><div class="row-title">Витрати</div><div class="row-meta">Додати, перейменувати, видалити</div></div>
          <div class="row-chevron">›</div></div>
        <div class="row" data-go="income_cats"><div class="avatar">+</div>
          <div><div class="row-title">Доходи</div><div class="row-meta">Додати, перейменувати, видалити</div></div>
          <div class="row-chevron">›</div></div>
        <div class="row" data-go="time_cats"><div class="avatar">T</div>
          <div><div class="row-title">Час</div><div class="row-meta">Активності для трекінгу</div></div>
          <div class="row-chevron">›</div></div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Команда</div></div>
      <div class="row-list">
        <div class="row" data-go="employees"><div class="avatar">P</div>
          <div><div class="row-title">Працівники</div><div class="row-meta">Список для ROI-звіту</div></div>
          <div class="row-chevron">›</div></div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Податки</div></div>
      <div class="row-list">
        <div class="row" data-go="tax"><div class="avatar">%</div>
          <div><div class="row-title">ФОП 3 група</div><div class="row-meta">Ставка єдиного податку та ЄСВ</div></div>
          <div class="row-chevron">›</div></div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Курси валют (НБУ)</div></div>
      <div class="row-list">
        <div class="row"><div class="avatar">$</div>
          <div><div class="row-title">USD</div><div class="row-meta">за 1 долар</div></div>
          <div class="amount">${esc(Number(rates.USD).toFixed(2))} ₴</div></div>
        <div class="row"><div class="avatar">€</div>
          <div><div class="row-title">EUR</div><div class="row-meta">за 1 євро</div></div>
          <div class="amount">${esc(Number(rates.EUR).toFixed(2))} ₴</div></div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Приватність</div></div>
      <div class="row-list">
        <div class="row" data-go="privacy"><div class="avatar">i</div>
          <div><div class="row-title">Як зберігаються дані</div><div class="row-meta">Railway Volume, ізоляція за Telegram ID</div></div>
          <div class="row-chevron">›</div></div>
      </div>
    </div>

    <div class="setting-section">
      <button class="btn btn-ghost" id="closeApp">Закрити Mini App</button>
    </div>
  `;

  root.querySelectorAll('[data-go]').forEach((el) => {
    el.addEventListener('click', () => {
      state.section = el.dataset.go;
      Telegram.haptic('selection');
      renderSettings();
    });
  });
  root.querySelector('#closeApp')?.addEventListener('click', () => Telegram.close());
}

// ── Categories editor (expense / income) ───────────────────────
async function renderCategoriesEditor(root, type, label) {
  root.innerHTML = backHeader(label);
  wireBack(root);

  const body = document.createElement('div');
  body.innerHTML = `<div class="panel" style="padding: var(--sp-4);"><div class="sk" style="height:80px;"></div></div>`;
  root.appendChild(body);

  try {
    const full = await Api.categoriesFull();
    const cats = full?.[type] || {};

    const addBox = `
      <div class="panel" style="padding: var(--sp-4); margin-bottom: var(--sp-3);">
        <div class="field" style="margin-bottom: var(--sp-2);">
          <label>Нова категорія</label>
          <input class="input" id="newCatName" placeholder="напр. Підписки">
        </div>
        <button class="btn btn-primary" id="addCatBtn">Додати категорію</button>
      </div>`;

    const list = `
      <div class="section-head"><div class="section-title">Існуючі</div></div>
      <div class="row-list">
        ${Object.entries(cats).map(([name, def]) => `
          <div class="row">
            <div class="avatar">${esc(def?.emoji || '•')}</div>
            <div>
              <div class="row-title">${esc(name)}</div>
              <div class="row-meta">${(def?.keywords || []).slice(0, 3).map(esc).join(', ') || '—'}</div>
            </div>
            ${name === 'Інше' ? '<div class="row-chevron">🔒</div>' : `<button class="ghost-btn delete-cat" data-name="${esc(name)}" aria-label="Видалити">×</button>`}
          </div>`).join('')}
      </div>`;

    body.innerHTML = addBox + list;

    body.querySelector('#addCatBtn').addEventListener('click', async () => {
      const name = body.querySelector('#newCatName').value.trim();
      if (!name) { toast('Введіть назву'); return; }
      try {
        await Api.addCategory({ type, name, keywords: [] });
        Telegram.haptic('success');
        toast('Категорію додано');
        renderCategoriesEditor(root, type, label);
      } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
    });

    body.querySelectorAll('.delete-cat').forEach((b) => {
      b.addEventListener('click', async () => {
        const name = b.dataset.name;
        try {
          await Api.deleteCategory(type, name);
          Telegram.haptic('warning');
          toast(`«${name}» видалено`);
          renderCategoriesEditor(root, type, label);
        } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
      });
    });
  } catch (e) {
    body.innerHTML = `<div class="empty-state" style="padding: var(--sp-4);"><div class="icon">!</div><p>${esc(e.message || 'Помилка')}</p></div>`;
  }
}

// ── Time categories editor ─────────────────────────────────────
async function renderTimeCategories(root) {
  root.innerHTML = backHeader('Категорії часу');
  wireBack(root);
  const body = document.createElement('div');
  body.innerHTML = `<div class="panel" style="padding: var(--sp-4);"><div class="sk" style="height:80px;"></div></div>`;
  root.appendChild(body);

  try {
    const cats = await Api.timeCategories();
    const addBox = `
      <div class="panel" style="padding: var(--sp-4); margin-bottom: var(--sp-3);">
        <div class="field" style="margin-bottom: var(--sp-2);">
          <label>Нова активність</label>
          <input class="input" id="newTcName" placeholder="напр. Медитація">
        </div>
        <div class="field" style="margin-bottom: var(--sp-2);">
          <label>Емодзі (необов'язково)</label>
          <input class="input" id="newTcEmoji" placeholder="🧘" maxlength="3">
        </div>
        <button class="btn btn-primary" id="addTcBtn">Додати</button>
      </div>`;
    const list = `
      <div class="section-head"><div class="section-title">Активності</div></div>
      <div class="row-list">
        ${Object.entries(cats || {}).map(([name, def]) => `
          <div class="row">
            <div class="avatar">${esc(def?.emoji || '⏱')}</div>
            <div><div class="row-title">${esc(name)}</div></div>
            ${name === 'Інше' ? '<div class="row-chevron">🔒</div>' : `<button class="ghost-btn delete-tc" data-name="${esc(name)}" aria-label="Видалити">×</button>`}
          </div>`).join('')}
      </div>`;
    body.innerHTML = addBox + list;

    body.querySelector('#addTcBtn').addEventListener('click', async () => {
      const name = body.querySelector('#newTcName').value.trim();
      const emoji = body.querySelector('#newTcEmoji').value.trim() || '⏱️';
      if (!name) { toast('Введіть назву'); return; }
      try {
        await Api.addTimeCategory(name, emoji);
        Telegram.haptic('success');
        toast('Додано');
        renderTimeCategories(root);
      } catch (e) { Telegram.haptic('error'); toast(e.message); }
    });

    body.querySelectorAll('.delete-tc').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          await Api.deleteTimeCategory(b.dataset.name);
          Telegram.haptic('warning');
          toast('Видалено');
          renderTimeCategories(root);
        } catch (e) { Telegram.haptic('error'); toast(e.message); }
      });
    });
  } catch (e) {
    body.innerHTML = `<div class="empty-state"><p>${esc(e.message)}</p></div>`;
  }
}

// ── Employees editor ───────────────────────────────────────────
async function renderEmployees(root) {
  root.innerHTML = backHeader('Працівники');
  wireBack(root);
  const body = document.createElement('div');
  body.innerHTML = `<div class="panel" style="padding: var(--sp-4);"><div class="sk" style="height:80px;"></div></div>`;
  root.appendChild(body);

  try {
    const list = await Api.employees();
    const addBox = `
      <div class="panel" style="padding: var(--sp-4); margin-bottom: var(--sp-3);">
        <div class="field" style="margin-bottom: var(--sp-2);">
          <label>Новий працівник</label>
          <input class="input" id="newEmp" placeholder="Імʼя">
        </div>
        <button class="btn btn-primary" id="addEmpBtn">Додати</button>
        <div class="ai-card-sub" style="margin-top: var(--sp-3);">
          При додаванні автоматично створюються категорії «Від &lt;ім'я&gt;» (дохід) та «ЗП &lt;ім'я&gt;» (витрата) — щоб ROI рахувався правильно.
        </div>
      </div>`;
    const empList = `
      <div class="section-head"><div class="section-title">Команда</div></div>
      <div class="row-list">
        ${(list || []).map((name) => `
          <div class="row">
            <div class="avatar">${esc((name?.[0] || '?').toUpperCase())}</div>
            <div><div class="row-title">${esc(name)}</div><div class="row-meta">авто-категорії: Від ${esc(name)} · ЗП ${esc(name)}</div></div>
            <button class="ghost-btn delete-emp" data-name="${esc(name)}" aria-label="Видалити">×</button>
          </div>`).join('')}
      </div>`;
    body.innerHTML = addBox + empList;

    body.querySelector('#addEmpBtn').addEventListener('click', async () => {
      const name = body.querySelector('#newEmp').value.trim();
      if (!name) { toast('Введіть ім\'я'); return; }
      try {
        await Api.addEmployee(name);
        Telegram.haptic('success');
        toast(`«${name}» додано`);
        renderEmployees(root);
      } catch (e) { Telegram.haptic('error'); toast(e.message); }
    });
    body.querySelectorAll('.delete-emp').forEach((b) => {
      b.addEventListener('click', async () => {
        try {
          await Api.deleteEmployee(b.dataset.name);
          Telegram.haptic('warning');
          toast('Видалено');
          renderEmployees(root);
        } catch (e) { Telegram.haptic('error'); toast(e.message); }
      });
    });
  } catch (e) {
    body.innerHTML = `<div class="empty-state"><p>${esc(e.message)}</p></div>`;
  }
}

// ── Tax settings ───────────────────────────────────────────────
async function renderTaxSettings(root) {
  root.innerHTML = backHeader('Податкові налаштування');
  wireBack(root);
  const body = document.createElement('div');
  body.innerHTML = `<div class="panel" style="padding: var(--sp-4);"><div class="sk" style="height:80px;"></div></div>`;
  root.appendChild(body);

  try {
    const s = await Api.settings();
    const tax = s?.tax_config || { single_tax_rate: 0.05, esv_fixed: 1760 };
    body.innerHTML = `
      <div class="panel" style="padding: var(--sp-4);">
        <div class="field">
          <label>Ставка єдиного податку (%)</label>
          <input class="input" id="taxRate" type="number" step="0.1" min="0" max="100" value="${(tax.single_tax_rate * 100).toFixed(1)}">
        </div>
        <div class="field">
          <label>Фіксований ЄСВ (₴/міс)</label>
          <input class="input" id="taxEsv" type="number" step="1" min="0" value="${tax.esv_fixed}">
        </div>
        <button class="btn btn-primary" id="saveTaxBtn" style="margin-top: var(--sp-3);">Зберегти</button>
      </div>
      <div class="panel ai-card" style="margin-top: var(--sp-3);">
        <div class="ai-card-row">
          <div class="ai-card-icon">📋</div>
          <div class="ai-card-text">
            <div class="ai-card-title">За замовчуванням — ФОП 3 група</div>
            <div class="ai-card-sub">Єдиний податок 5% від доходу + ЄСВ 1 760 ₴/міс. Якщо у вас 1 чи 2 група — змініть тут.</div>
          </div>
        </div>
      </div>
    `;
    body.querySelector('#saveTaxBtn').addEventListener('click', async () => {
      const rate = parseFloat(body.querySelector('#taxRate').value) / 100;
      const esv = parseFloat(body.querySelector('#taxEsv').value);
      if (!isFinite(rate) || rate < 0 || rate > 1) { toast('Невірна ставка'); return; }
      if (!isFinite(esv) || esv < 0) { toast('Невірний ЄСВ'); return; }
      try {
        await Api.patchTax({ single_tax_rate: rate, esv_fixed: esv });
        Telegram.haptic('success');
        toast('Збережено');
      } catch (e) { Telegram.haptic('error'); toast(e.message); }
    });
  } catch (e) {
    body.innerHTML = `<div class="empty-state"><p>${esc(e.message)}</p></div>`;
  }
}

// ── Privacy ────────────────────────────────────────────────────
function renderPrivacy(root) {
  root.innerHTML = backHeader('Приватність');
  wireBack(root);
  const body = document.createElement('div');
  body.innerHTML = `
    <div class="panel" style="padding: var(--sp-4);">
      <p style="color: var(--ruby-ivory); font-weight: 700; margin-top: 0;">Де зберігаються дані</p>
      <p style="color: var(--ruby-muted); font-size: var(--fs-13); line-height: 1.6;">
        Усі ваші транзакції, час і налаштування зберігаються в SQLite-базі на Railway Volume у EU-регіоні.
        Дані ізольовано за вашим Telegram ID — інші користувачі їх не бачать.
      </p>
      <p style="color: var(--ruby-ivory); font-weight: 700; margin-top: var(--sp-4);">Хто має доступ</p>
      <p style="color: var(--ruby-muted); font-size: var(--fs-13); line-height: 1.6;">
        Тільки ви та адміністратор сервісу для технічного супроводу та щодобових бекапів.
        AI-сервіси (ChatGPT, Claude) дані НЕ отримують — все що передається їм, ви робите вручну.
      </p>
      <p style="color: var(--ruby-ivory); font-weight: 700; margin-top: var(--sp-4);">Право на видалення</p>
      <p style="color: var(--ruby-muted); font-size: var(--fs-13); line-height: 1.6;">
        Нижче — кнопка «Очистити всі мої дані». Це фінально, без відновлення.
      </p>
    </div>
    <div class="panel" style="padding: var(--sp-4); margin-top: var(--sp-3); border-color: rgba(212, 90, 79, 0.4);">
      <button class="btn btn-secondary" id="clearAllBtn" style="background: rgba(212, 90, 79, 0.18); color: var(--ruby-danger); border-color: rgba(212, 90, 79, 0.4);">
        🗑 Очистити всі мої дані
      </button>
    </div>
  `;
  root.appendChild(body);
  body.querySelector('#clearAllBtn').addEventListener('click', () => {
    Telegram.haptic('warning');
    toast('Очищення — поки тільки через бот: /очистити');
  });
}

// ── Main entry ─────────────────────────────────────────────────
export function renderSettings() {
  const root = document.getElementById('screen-settings');
  if (!root) return;
  switch (state.section) {
    case 'expense_cats': renderCategoriesEditor(root, 'expense', 'Категорії витрат'); break;
    case 'income_cats':  renderCategoriesEditor(root, 'income', 'Категорії доходів'); break;
    case 'time_cats':    renderTimeCategories(root); break;
    case 'employees':    renderEmployees(root); break;
    case 'tax':          renderTaxSettings(root); break;
    case 'privacy':      renderPrivacy(root); break;
    default:             renderMain(root);
  }
}
