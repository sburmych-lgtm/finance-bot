/* Settings screen — full CRUD: categories, employees, time categories, tax, danger zone */

import { Store, navigate } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { toast, esc, setHTML } from '../ui.js';
import {
  ACCOUNT_DELETE_CONFIRMATION,
  isAccountDeleteConfirmation,
} from '../privacy.js';

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
          <div><div class="row-title">Податковий профіль</div><div class="row-meta">Група, ставки та правила за роками</div></div>
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
        ${Object.entries(cats).map(([name, def]) => {
          const subs = Array.isArray(def?.subcategories) ? def.subcategories : [];
          const isOpen = _openCat === name;
          return `
          <div class="cat-block">
            <div class="row">
              <div class="avatar">${esc(def?.emoji || '•')}</div>
              <div class="cat-main" data-toggle-sub="${esc(name)}" style="cursor:pointer;">
                <div class="row-title">${esc(name)} ${subs.length ? `<span class="sub-count">${subs.length}</span>` : ''}</div>
                <div class="row-meta">${subs.length ? `${subs.length} підрозділ(ів) · торкніться, щоб відкрити` : 'Торкніться, щоб додати підрозділи'}</div>
              </div>
              ${name === 'Інше' ? '<div class="row-chevron">🔒</div>' : `
                <div style="display:flex; gap:var(--sp-1);">
                  <button class="ghost-btn rename-cat" data-name="${esc(name)}" aria-label="Перейменувати">✎</button>
                  <button class="ghost-btn delete-cat" data-name="${esc(name)}" aria-label="Видалити">×</button>
                </div>`}
            </div>
            ${isOpen ? `
              <div class="sub-editor">
                <div class="chip-grid" style="margin-bottom: var(--sp-2);">
                  ${subs.map((s) => `
                    <span class="chip sub-chip-edit">
                      ${esc(s)}
                      <button class="sub-del" data-sub-del="${esc(s)}" data-sub-cat="${esc(name)}" aria-label="Видалити">×</button>
                    </span>`).join('') || '<span class="row-meta" style="padding:4px 0;">Ще немає підрозділів</span>'}
                </div>
                <div class="sub-add-row">
                  <input class="input" data-sub-input="${esc(name)}" placeholder="напр. Комунальні, Оренда">
                  <button class="btn btn-secondary sub-add-btn" data-sub-add="${esc(name)}">Додати</button>
                </div>
              </div>` : ''}
          </div>`;
        }).join('')}
      </div>`;

    setHTML(body, addBox + list);

    body.querySelector('#addCatBtn').addEventListener('click', async () => {
      const name = body.querySelector('#newCatName').value.trim();
      if (!name) { toast('Введіть назву'); return; }
      try {
        await Api.addCategory({ type, name, keywords: [] });
        await Store.hydrate();
        Telegram.haptic('success');
        toast('Категорію додано');
        renderCategoriesEditor(root, type, label);
      } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
    });

    body.querySelectorAll('.delete-cat').forEach((b) => {
      b.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = b.dataset.name;
        if (!window.confirm(`Видалити категорію «${name}» з усіма підрозділами?`)) return;
        try {
          await Api.deleteCategory(type, name);
          await Store.hydrate();
          Telegram.haptic('warning');
          toast(`«${name}» видалено`);
          if (_openCat === name) _openCat = null;
          renderCategoriesEditor(root, type, label);
        } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
      });
    });

    body.querySelectorAll('.rename-cat').forEach((b) => {
      b.addEventListener('click', async (e) => {
        e.stopPropagation();
        const name = b.dataset.name;
        const newName = window.prompt('Нова назва категорії', name)?.trim();
        if (!newName || newName === name) return;
        try {
          await Api.patchCategory(type, name, { new_name: newName });
          await Store.hydrate();
          Telegram.haptic('success');
          toast(`«${name}» перейменовано на «${newName}»`);
          if (_openCat === name) _openCat = newName;
          renderCategoriesEditor(root, type, label);
        } catch (err) { Telegram.haptic('error'); toast(err.message || 'Помилка'); }
      });
    });

    // Expand/collapse a category to manage its subcategories
    body.querySelectorAll('[data-toggle-sub]').forEach((el) => {
      el.addEventListener('click', () => {
        const name = el.dataset.toggleSub;
        _openCat = (_openCat === name) ? null : name;
        Telegram.haptic('selection');
        renderCategoriesEditor(root, type, label);
      });
    });

    // Add subcategory
    body.querySelectorAll('[data-sub-add]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const cat = btn.dataset.subAdd;
        const input = body.querySelector(`[data-sub-input="${CSS.escape(cat)}"]`);
        const subName = (input?.value || '').trim();
        if (!subName) { toast('Введіть назву підрозділу'); return; }
        try {
          await Api.addSubcategory(type, cat, subName);
          await Store.hydrate();
          Telegram.haptic('success');
          toast('Підрозділ додано');
          _openCat = cat;
          renderCategoriesEditor(root, type, label);
        } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
      });
    });

    // Delete subcategory
    body.querySelectorAll('[data-sub-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const cat = btn.dataset.subCat;
        const sub = btn.dataset.subDel;
        try {
          await Api.deleteSubcategory(type, cat, sub);
          await Store.hydrate();
          Telegram.haptic('warning');
          toast('Підрозділ видалено');
          _openCat = cat;
          renderCategoriesEditor(root, type, label);
        } catch (e) { Telegram.haptic('error'); toast(e.message || 'Помилка'); }
      });
    });
  } catch (e) {
    setHTML(body, `<div class="empty-state" style="padding: var(--sp-4);"><div class="icon">!</div><p>${esc(e.message || 'Помилка')}</p></div>`);
  }
}

// Which category is currently expanded in the editor (module-level so it
// survives the re-renders triggered by add/delete).
let _openCat = null;

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
  { id: 'fop1', label: 'ФОП 1 група',     hint: 'Фіксований єдиний податок, ЄСВ і військовий збір' },
  { id: 'fop2', label: 'ФОП 2 група',     hint: 'Фіксований єдиний податок, ЄСВ і військовий збір' },
  { id: 'fop3', label: 'ФОП 3 група',     hint: '5% без ПДВ або 3% + ПДВ, ЄСВ і 1% військового збору' },
  { id: 'none', label: 'Я не ФОП',        hint: 'Фізособа — нічого не нараховуємо' },
];

async function renderTaxSettings(root, selectedTaxYear = null) {
  root.innerHTML = backHeader('Податкові налаштування');
  wireBack(root);
  const body = document.createElement('div');
  body.innerHTML = `<div class="panel" style="padding: var(--sp-4);"><div class="sk" style="height:80px;"></div></div>`;
  root.appendChild(body);

  try {
    const s = await Api.settings();
    const tax = s?.tax_config || {};
    const supportedYears = Array.isArray(s?.supported_tax_years)
      ? s.supported_tax_years.map(Number).filter(Number.isFinite)
      : [Number(s?.tax_year || new Date().getFullYear())];
    const defaultYear = Number(s?.tax_year || supportedYears.at(-1));
    const taxYear = supportedYears.includes(Number(selectedTaxYear))
      ? Number(selectedTaxYear)
      : defaultYear;
    const profile = s?.tax_profiles?.[String(taxYear)]
      || (taxYear === defaultYear ? s?.tax_profile : null)
      || tax?.profiles_by_year?.[String(taxYear)]
      || tax;
    const group = profile.group || 'fop3';
    const scheme = profile.scheme || '5_percent';
    const fop1 = profile.fop1_fixed ?? 332.80;
    const fop2 = profile.fop2_fixed ?? 1729.40;
    const esv = profile.esv_fixed ?? 1902.34;
    const militaryFixed = profile.military_fixed ?? 864.70;
    const militaryRate = profile.military_rate ?? 0.01;
    const draft = { scheme, fop1, fop2, esv, militaryFixed, militaryRate, taxYear };

    const groupCards = TAX_GROUPS.map((g) => `
      <button class="tax-group-card ${group === g.id ? 'active' : ''}" data-group="${esc(g.id)}">
        <div class="tax-group-label">${esc(g.label)}</div>
        <div class="tax-group-hint">${esc(g.hint)}</div>
      </button>
    `).join('');

    body.innerHTML = `
      <div class="panel" style="padding: var(--sp-4); margin-bottom: var(--sp-3);">
        <label for="taxYearSelect" style="font-size:10px; letter-spacing:.14em; text-transform:uppercase; font-weight:800; color: var(--ruby-gold); display:block; margin-bottom: var(--sp-2);">Рік податкових правил</label>
        <select class="input" id="taxYearSelect">
          ${supportedYears.map((year) => `<option value="${esc(String(year))}" ${year === taxYear ? 'selected' : ''}>${esc(String(year))}</option>`).join('')}
        </select>
      </div>

      <div class="panel" style="padding: var(--sp-4);">
        <label style="font-size:10px; letter-spacing:.14em; text-transform:uppercase; font-weight:800; color: var(--ruby-gold); display:block; margin-bottom: var(--sp-2);">Оберіть групу · правила ${esc(String(taxYear))}</label>
        <div class="tax-group-grid">${groupCards}</div>
      </div>

      <div class="panel" style="padding: var(--sp-4); margin-top: var(--sp-3);" id="taxFieldsPanel">
        ${renderTaxFields(group, draft)}
      </div>

      <button class="btn btn-primary" id="saveTaxBtn" style="margin-top: var(--sp-3);">Зберегти</button>

      <div class="panel ai-card" style="margin-top: var(--sp-3);">
        <div class="ai-card-row">
          <div class="ai-card-icon">📋</div>
          <div class="ai-card-text">
            <div class="ai-card-title">Як це впливає на звіти</div>
            <div class="ai-card-sub">Обрана група визначає формулу в «Звіти → Податки». Розрахунок інформаційний, не є податковою консультацією та не враховує спеціальні пільги. Для схеми 3% + ПДВ сума ПДВ не розраховується.</div>
          </div>
        </div>
      </div>
    `;

    body.querySelector('#taxYearSelect')?.addEventListener('change', (event) => {
      renderTaxSettings(root, Number(event.target.value));
    });

    // Reactive group selector — re-renders fields panel when group changes
    body.querySelectorAll('[data-group]').forEach((btn) => {
      btn.addEventListener('click', () => {
        body.querySelectorAll('[data-group]').forEach((b) => b.classList.toggle('active', b === btn));
        const newGroup = btn.dataset.group;
        captureTaxDraft(body, draft);
        body.querySelector('#taxFieldsPanel').innerHTML =
          renderTaxFields(newGroup, draft);
        Telegram.haptic('selection');
      });
    });

    body.querySelector('#saveTaxBtn').addEventListener('click', async () => {
      const activeGroup = body.querySelector('[data-group].active')?.dataset.group || 'fop3';
      const payload = { year: taxYear, group: activeGroup };
      captureTaxDraft(body, draft);

      if (activeGroup === 'fop3') {
        const schemeValue = body.querySelector('#taxScheme')?.value;
        if (!['5_percent', '3_percent_vat'].includes(schemeValue)) { toast('Оберіть податкову схему'); return; }
        payload.scheme = schemeValue;
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

function captureTaxDraft(root, draft) {
  const fields = [
    ['taxFop1', 'fop1'],
    ['taxFop2', 'fop2'],
    ['taxEsv', 'esv'],
  ];
  fields.forEach(([id, key]) => {
    const input = root.querySelector(`#${id}`);
    if (!input) return;
    const value = parseFloat(input.value);
    if (Number.isFinite(value)) draft[key] = value;
  });
  const scheme = root.querySelector('#taxScheme')?.value;
  if (['5_percent', '3_percent_vat'].includes(scheme)) draft.scheme = scheme;
}

function renderTaxFields(group, { scheme, fop1, fop2, esv, militaryFixed, militaryRate, taxYear }) {
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
        <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">До 10% прожиткового мінімуму. Максимум за правилами ${esc(String(taxYear))} року — ${esc(String(fop1).replace('.', ','))} ₴; місцева ставка може бути нижчою.</p>
      </div>`;
  } else if (group === 'fop2') {
    html += `
      <div class="field">
        <label>Єдиний податок (₴/міс)</label>
        <input class="input" id="taxFop2" type="number" step="1" min="0" max="20000" value="${esc(String(fop2))}">
        <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">До 20% мінімальної зарплати. Максимум за правилами ${esc(String(taxYear))} року — ${esc(Number(fop2).toLocaleString('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))} ₴; місцева ставка може бути нижчою.</p>
      </div>`;
  } else {  // fop3
    html += `
      <div class="field">
        <label>Схема єдиного податку</label>
        <select class="input" id="taxScheme">
          <option value="5_percent" ${scheme === '5_percent' ? 'selected' : ''}>5% без ПДВ</option>
          <option value="3_percent_vat" ${scheme === '3_percent_vat' ? 'selected' : ''}>3% + ПДВ</option>
        </select>
        <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">Для 3% + ПДВ звіт рахує єдиний податок, але не суму ПДВ.</p>
      </div>`;
  }
  html += `
    <div class="field">
      <label>Фіксований ЄСВ (₴/міс)</label>
      <input class="input" id="taxEsv" type="number" step="1" min="0" max="50000" value="${esc(String(esv))}">
      <p style="color:var(--ruby-muted); font-size:11px; margin: 4px 0 0;">22% × мінімальна зарплата. Мінімальний платіж за правилами ${esc(String(taxYear))} року — ${esc(Number(esv).toLocaleString('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }))} ₴.</p>
    </div>
    <div class="field">
      <label>Військовий збір</label>
      <div class="input" style="display:flex; align-items:center; opacity:.86;">
        ${group === 'fop3' ? `${esc(String(militaryRate * 100))}% від доходу` : `${esc(String(militaryFixed.toFixed(2)))} ₴/міс`}
      </div>
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
        Усі ваші транзакції, час і налаштування зберігаються в SQLite-базі на інфраструктурі Railway.
        Дані ізольовано за вашим Telegram ID — інші користувачі їх не бачать.
      </p>
      <p style="color: var(--ruby-ivory); font-weight: 700; margin-top: var(--sp-4);">Хто має доступ</p>
      <p style="color: var(--ruby-muted); font-size: var(--fs-13); line-height: 1.6;">
        З боку Ruby Finance доступ має лише адміністратор у межах технічного супроводу.
        AI-сервіси (ChatGPT, Claude) дані НЕ отримують — все що передається їм, ви робите вручну.
      </p>
      <p style="color: var(--ruby-ivory); font-weight: 700; margin-top: var(--sp-4);">Право на видалення</p>
      <p style="color: var(--ruby-muted); font-size: var(--fs-13); line-height: 1.6;">
        Ви можете назавжди видалити профіль, фінансові операції, записи часу,
        налаштування та статус підписки у «Небезпечній зоні» нижче.
      </p>
      <div class="legal-links" aria-label="Юридичні документи">
        <a href="/privacy" target="_blank" rel="noopener noreferrer">Політика приватності</a>
        <a href="/terms" target="_blank" rel="noopener noreferrer">Умови користування</a>
      </div>
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
    <div class="panel danger-zone" aria-labelledby="deleteAccountTitle">
      <div class="danger-zone-kicker">Небезпечна зона</div>
      <h3 id="deleteAccountTitle">Видалити акаунт і всі дані</h3>
      <p>
        Цю дію неможливо скасувати. Для підтвердження введіть
        <strong>${ACCOUNT_DELETE_CONFIRMATION}</strong> без пробілів.
      </p>
      <label class="danger-confirm-label" for="deleteAccountConfirmation">
        Підтвердження
      </label>
      <input
        class="input danger-confirm-input"
        id="deleteAccountConfirmation"
        type="text"
        autocomplete="off"
        autocapitalize="characters"
        spellcheck="false"
        aria-describedby="deleteAccountHint deleteAccountStatus"
        placeholder="${ACCOUNT_DELETE_CONFIRMATION}"
      >
      <div class="danger-confirm-hint" id="deleteAccountHint">
        Буде видалено лише ваш акаунт. Дані інших користувачів не зміняться.
      </div>
      <div class="danger-status" id="deleteAccountStatus" role="status" aria-live="polite"></div>
      <button class="btn danger-delete-btn" id="deleteAccountBtn" disabled>
        Назавжди видалити акаунт
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

  const confirmationInput = body.querySelector('#deleteAccountConfirmation');
  const deleteButton = body.querySelector('#deleteAccountBtn');
  const deleteStatus = body.querySelector('#deleteAccountStatus');
  let deleting = false;

  const showDeleteStatus = (message, kind = '') => {
    deleteStatus.textContent = message;
    deleteStatus.className = `danger-status${kind ? ` ${kind}` : ''}`;
  };

  const syncDeleteButton = () => {
    const confirmed = isAccountDeleteConfirmation(confirmationInput.value);
    deleteButton.disabled = deleting || !confirmed;
    deleteButton.setAttribute('aria-busy', deleting ? 'true' : 'false');
  };

  confirmationInput.addEventListener('input', () => {
    if (!deleting) showDeleteStatus('');
    syncDeleteButton();
  });

  deleteButton.addEventListener('click', async () => {
    if (deleting || !isAccountDeleteConfirmation(confirmationInput.value)) {
      showDeleteStatus(`Введіть точно ${ACCOUNT_DELETE_CONFIRMATION}.`, 'error');
      syncDeleteButton();
      return;
    }

    const approved = window.confirm(
      'Назавжди видалити ваш акаунт, усі фінансові операції, записи часу та налаштування?'
    );
    if (!approved) return;

    deleting = true;
    confirmationInput.disabled = true;
    deleteButton.textContent = 'Видаляємо…';
    showDeleteStatus('Видаляємо ваші дані. Не закривайте Mini App…');
    syncDeleteButton();

    try {
      await Api.deleteAccount(ACCOUNT_DELETE_CONFIRMATION);
      Telegram.haptic('success');
      deleteButton.textContent = 'Акаунт видалено';
      showDeleteStatus('Ваш акаунт і дані видалено. Mini App можна закрити.', 'success');
      window.setTimeout(() => Telegram.close(), 1600);
    } catch (error) {
      deleting = false;
      confirmationInput.disabled = false;
      deleteButton.textContent = 'Повторити видалення';
      showDeleteStatus(error.message || 'Не вдалося видалити акаунт. Спробуйте ще раз.', 'error');
      Telegram.haptic('error');
      syncDeleteButton();
    }
  });

  syncDeleteButton();
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
