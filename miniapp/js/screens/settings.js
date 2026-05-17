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
const TAX_GROUPS = [
  { id: 'fop1', label: 'ФОП 1 група',     hint: 'Фіксований податок ~10% прожиткового мінімуму + ЄСВ' },
  { id: 'fop2', label: 'ФОП 2 група',     hint: 'Фіксований податок ~20% мінімалки + ЄСВ' },
  { id: 'fop3', label: 'ФОП 3 група',     hint: '% від доходу + ЄСВ. Найпоширеніше для самозайнятих' },
  { id: 'none', label: 'Я не ФОП',        hint: 'Фізособа — нічого не нараховуємо' },
];

async function renderTaxSettings(root) {
  root.innerHTML = backHeader('Податкові налаштування');
  wireBack(root);
  const body = document.createElement('div');
  body.innerHTML = `<div class="panel" style="padding: var(--sp-4);"><div class="sk" style="height:80px;"></div></div>`;
  root.appendChild(body);

  try {
    const s = await Api.settings();
    const tax = s?.tax_config || {};
    const group = tax.group || 'fop3';
    const rate = (tax.single_tax_rate ?? 0.05) * 100;
    const fop1 = tax.fop1_fixed ?? 303;
    const fop2 = tax.fop2_fixed ?? 1600;
    const esv = tax.esv_fixed ?? 1760;

    const groupCards = TAX_GROUPS.map((g) => `
      <button class="tax-group-card ${group === g.id ? 'active' : ''}" data-group="${esc(g.id)}">
        <div class="tax-group-label">${esc(g.label)}</div>
        <div class="tax-group-hint">${esc(g.hint)}</div>
      </button>
    `).join('');

    body.innerHTML = `
      <div class="panel" style="padding: var(--sp-4);">
        <label style="font-size:10px; letter-spacing:.14em; text-transform:uppercase; font-weight:800; color: var(--ruby-gold); display:block; margin-bottom: var(--sp-2);">Оберіть групу</label>
        <div class="tax-group-grid">${groupCards}</div>
      </div>

      <div class="panel" style="padding: var(--sp-4); margin-top: var(--sp-3);" id="taxFieldsPanel">
        ${renderTaxFields(group, { rate, fop1, fop2, esv })}
      </div>

      <button class="btn btn-primary" id="saveTaxBtn" style="margin-top: var(--sp-3);">Зберегти</button>

      <div class="panel ai-card" style="margin-top: var(--sp-3);">
        <div class="ai-card-row">
          <div class="ai-card-icon">📋</div>
          <div class="ai-card-text">
            <div class="ai-card-title">Як це впливає на звіти</div>
            <div class="ai-card-sub">Обрана група визначає формулу в «Звіти → Податки». Цифри 2026 — приблизні, перевірте на сайті ДПС перед поданням декларації.</div>
          </div>
        </div>
      </div>
    `;

    // Reactive group selector — re-renders fields panel when group changes
    body.querySelectorAll('[data-group]').forEach((btn) => {
      btn.addEventListener('click', () => {
        body.querySelectorAll('[data-group]').forEach((b) => b.classList.toggle('active', b === btn));
        const newGroup = btn.dataset.group;
        body.querySelector('#taxFieldsPanel').innerHTML =
          renderTaxFields(newGroup, { rate: getCurrentRate(body), fop1: getCurrentFop1(body), fop2: getCurrentFop2(body), esv: getCurrentEsv(body) });
        Telegram.haptic('selection');
      });
    });

    body.querySelector('#saveTaxBtn').addEventListener('click', async () => {
      const activeGroup = body.querySelector('[data-group].active')?.dataset.group || 'fop3';
      const payload = { group: activeGroup };

      if (activeGroup === 'fop3') {
        const r = parseFloat(body.querySelector('#taxRate')?.value);
        if (!isFinite(r) || r < 1 || r > 25) { toast('Ставка має бути 1–25%'); return; }
        payload.single_tax_rate = r / 100;
      }
      if (activeGroup === 'fop1') {
        const v = parseFloat(body.querySelector('#taxFop1')?.value);
        if (!isFinite(v) || v < 0 || v > 10000) { toast('Невірна сума'); return; }
        payload.fop1_fixed = v;
      }
      if (activeGroup === 'fop2') {
        const v = parseFloat(body.querySelector('#taxFop2')?.value);
        if (!isFinite(v) || v < 0 || v > 20000) { toast('Невірна сума'); return; }
        payload.fop2_fixed = v;
      }
      if (activeGroup !== 'none') {
        const e = parseFloat(body.querySelector('#taxEsv')?.value);
        if (!isFinite(e) || e < 0 || e > 50000) { toast('Невірний ЄСВ'); return; }
        payload.esv_fixed = e;
      }

      try {
        await Api.patchTax(payload);
        Telegram.haptic('success');
        toast('Збережено');
      } catch (e) { Telegram.haptic('error'); toast(e.message); }
    });
  } catch (e) {
    body.innerHTML = `<div class="empty-state"><p>${esc(e.message)}</p></div>`;
  }
}

function getCurrentRate(root) { return parseFloat(root.querySelector('#taxRate')?.value) || 5; }
function getCurrentFop1(root) { return parseFloat(root.querySelector('#taxFop1')?.value) || 303; }
function getCurrentFop2(root) { return parseFloat(root.querySelector('#taxFop2')?.value) || 1600; }
function getCurrentEsv(root)  { return parseFloat(root.querySelector('#taxEsv')?.value)  || 1760; }

function renderTaxFields(group, { rate, fop1, fop2, esv }) {
  if (group === 'none') {
    return `
      <div class="empty-state" style="padding: var(--sp-4);">
        <div class="icon">∅</div>
        <h3>Без нарахувань</h3>
        <p>Як фізособа ви не сплачуєте єдиний податок та ЄСВ. Звіти «Податки» показуватимуть 0.</p>
      </div>
    `;
  }
  let html = '';
  if (group === 'fop1') {
    html += `
      <div class="field">
        <label>Єдиний податок (₴/міс)</label>
        <input class="input" id="taxFop1" type="number" step="1" min="0" max="10000" value="${esc(String(fop1))}">
        <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">10% прожиткового мінімуму. На 2026 ≈ 303 ₴.</p>
      </div>`;
  } else if (group === 'fop2') {
    html += `
      <div class="field">
        <label>Єдиний податок (₴/міс)</label>
        <input class="input" id="taxFop2" type="number" step="1" min="0" max="20000" value="${esc(String(fop2))}">
        <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">20% мінімалки. На 2026 = 1 600 ₴.</p>
      </div>`;
  } else {  // fop3
    html += `
      <div class="field">
        <label>Ставка єдиного податку (%)</label>
        <input class="input" id="taxRate" type="number" step="0.1" min="1" max="25" value="${esc(rate.toFixed(1))}">
        <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">5% — неплатники ПДВ. 3% — платники ПДВ.</p>
      </div>`;
  }
  html += `
    <div class="field">
      <label>Фіксований ЄСВ (₴/міс)</label>
      <input class="input" id="taxEsv" type="number" step="1" min="0" max="50000" value="${esc(String(esv))}">
      <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">22% × мінімалка. На 2026 = 1 760 ₴.</p>
    </div>`;
  return html;
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
    <div class="panel" style="padding: var(--sp-4); margin-top: var(--sp-3);">
      <button class="btn btn-secondary" id="resetBtn">
        ↺ Скинути налаштування до дефолтів
      </button>
      <p style="color: var(--ruby-muted); font-size: var(--fs-12); margin: var(--sp-3) 0 0; line-height: 1.5;">
        Скидає тільки <b>налаштування</b> (працівники, категорії, податки) до базового стану.
        Транзакції та час залишаються незмінними.
      </p>
    </div>
    <div class="panel" style="padding: var(--sp-4); margin-top: var(--sp-3); border-color: rgba(212, 90, 79, 0.4);">
      <button class="btn btn-secondary" id="clearAllBtn" style="background: rgba(212, 90, 79, 0.18); color: var(--ruby-danger); border-color: rgba(212, 90, 79, 0.4);">
        🗑 Очистити всі мої дані
      </button>
    </div>
  `;
  root.appendChild(body);
  body.querySelector('#resetBtn').addEventListener('click', async () => {
    if (!window.confirm('Скинути всі ваші налаштування (працівники, категорії, податки) до дефолтів? Транзакції залишаться.')) return;
    try {
      await Api.resetSettings();
      Telegram.haptic('success');
      toast('Налаштування скинуто');
      await Store.hydrate();
      state.section = 'main';
      renderSettings();
    } catch (e) {
      Telegram.haptic('error');
      toast(e.message || 'Помилка');
    }
  });
  body.querySelector('#clearAllBtn').addEventListener('click', () => {
    Telegram.haptic('warning');
    toast('Очищення всіх даних — поки тільки через бот: /очистити');
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
