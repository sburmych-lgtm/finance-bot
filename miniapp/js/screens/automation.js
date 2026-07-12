import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { esc, fmtAmount, fmtMoney, setHTML, toast } from '../ui.js';
import { paymentSourceLabel, paymentSourceOptions } from '../block2-ui.js';
import {
  buildRecurringPatch,
  normalizeDigest,
  normalizeNotificationSettings,
  normalizeRecurringOperations,
  normalizeRecurringSuggestions,
  recurrenceLabel,
} from '../automation-ui.js';

let renderGeneration = 0;

function header(label) {
  return `
    <div class="settings-back automation-back">
      <button type="button" class="ghost-btn" data-automation-back aria-label="Назад">‹</button>
      <div class="settings-back-title">${esc(label)}</div>
    </div>`;
}

function wireBack(root, onBack) {
  root.querySelector('[data-automation-back]')?.addEventListener('click', () => {
    Telegram.haptic('selection');
    onBack();
  });
}

function loading(label) {
  return `${header(label)}
    <div class="panel automation-loading" aria-busy="true">
      <div class="sk"></div><div class="sk"></div><div class="sk"></div>
    </div>`;
}

function errorMarkup(label, message) {
  return `${header(label)}
    <div class="empty-state automation-error" role="status">
      <div class="icon">!</div><h3>Не вдалося завантажити</h3>
      <p>${esc(message || 'Перевірте з’єднання та спробуйте ще раз.')}</p>
      <button type="button" class="btn btn-secondary automation-retry">Повторити</button>
    </div>`;
}

function localIsoDate(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function uiDate(value) {
  const parsed = new Date(`${value}T00:00:00`);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString('uk-UA', { day: 'numeric', month: 'short', year: 'numeric' });
}

function sourceOptions(selected) {
  return `
    <option value="" ${selected == null ? 'selected' : ''}>Не вказано</option>
    ${paymentSourceOptions().map(({ value, label }) =>
      `<option value="${value}" ${selected === value ? 'selected' : ''}>${esc(label)}</option>`
    ).join('')}`;
}

function categoryOptions(categories, selected = null) {
  return categories.map((category) =>
    `<option value="${esc(category)}" ${category === selected ? 'selected' : ''}>${esc(category)}</option>`
  ).join('');
}

function recurringCards(operations) {
  if (!operations.length) {
    return `<div class="empty-state recurring-empty">
      <div class="icon">↻</div><h3>Регулярних операцій ще немає</h3>
      <p>Додайте оренду, підписку, зарплату чи іншу повторювану операцію.</p>
    </div>`;
  }
  return `<div class="recurring-list">
    ${operations.map((operation) => `
      <article class="panel recurring-card ${operation.active ? '' : 'paused'}">
        <div class="recurring-card-head">
          <span class="avatar" aria-hidden="true">${operation.type === 'income' ? '+' : '−'}</span>
          <span>
            <strong>${esc(operation.description || operation.category)}</strong>
            <small>${esc(operation.category)}${operation.subcategory ? ` · ${esc(operation.subcategory)}` : ''}</small>
          </span>
          <span class="amount ${operation.type}">${esc(fmtAmount(operation.amount, operation.currency))}</span>
        </div>
        <div class="recurring-badges">
          <span>${esc(recurrenceLabel(operation.frequency, operation.interval))}</span>
          <span>${esc(paymentSourceLabel(operation.paymentSource))}</span>
          <span>${operation.autoCreate ? 'Автоматично' : 'Без автододавання'}</span>
          ${operation.active ? '' : '<span class="paused-badge">Призупинено</span>'}
        </div>
        <p class="recurring-next">Наступна дата: <strong>${esc(uiDate(operation.nextDueDate))}</strong></p>
        <div class="recurring-actions">
          <button type="button" class="btn btn-secondary" data-recurring-edit="${operation.id}">Змінити</button>
          <button type="button" class="btn btn-secondary" data-recurring-toggle="${operation.id}">${operation.active ? 'Призупинити' : 'Відновити'}</button>
          <button type="button" class="btn btn-ghost danger" data-recurring-delete="${operation.id}">Видалити</button>
        </div>
      </article>
    `).join('')}
  </div>`;
}

function suggestionCards(suggestions) {
  if (!suggestions.length) return '';
  return `
    <div class="section-head"><div class="section-title">Знайдені повторення</div><span class="section-link">потрібне підтвердження</span></div>
    <div class="suggestion-strip" aria-label="Пропозиції регулярних операцій">
      ${suggestions.map((suggestion, index) => `
        <button type="button" class="panel recurring-suggestion" data-suggestion-index="${index}">
          <span class="suggestion-icon" aria-hidden="true">✦</span>
          <span><strong>${esc(suggestion.description || suggestion.category)}</strong>
            <small>${esc(fmtAmount(suggestion.amount, suggestion.currency))} · ${esc(recurrenceLabel(suggestion.frequency))} · ${suggestion.occurrences} збіги</small>
          </span>
          <span aria-hidden="true">›</span>
        </button>
      `).join('')}
    </div>`;
}

function recurringForm(categoriesByType) {
  const expenses = Object.keys(categoriesByType.expense || {});
  return `
    <div class="panel recurring-editor">
      <div class="eyebrow">Нова автоматизація</div>
      <h3 id="recurringFormTitle">Регулярна операція</h3>
      <p>Шаблон створює операції за графіком. Перед збереженням усі поля можна змінити.</p>
      <form id="recurringForm" class="recurring-form">
        <div class="automation-form-grid two">
          <label class="field"><span>Тип</span><select class="input" id="recurringType">
            <option value="expense">Витрата</option><option value="income">Дохід</option>
          </select></label>
          <label class="field"><span>Сума</span><input class="input" id="recurringAmount" type="number" min="0.01" step="0.01" inputmode="decimal" required></label>
        </div>
        <div class="automation-form-grid two">
          <label class="field"><span>Валюта</span><select class="input" id="recurringCurrency">
            <option value="UAH">UAH ₴</option><option value="USD">USD $</option><option value="EUR">EUR €</option>
          </select></label>
          <label class="field"><span>Джерело коштів</span><select class="input" id="recurringSource">${sourceOptions(null)}</select></label>
        </div>
        <label class="field"><span>Категорія</span><select class="input" id="recurringCategory">${categoryOptions(expenses)}</select></label>
        <label class="field" id="recurringSubcategoryField"><span>Підрозділ (необов’язково)</span><select class="input" id="recurringSubcategory"><option value="">Без підрозділу</option></select></label>
        <label class="field"><span>Опис</span><input class="input" id="recurringDescription" maxlength="200" placeholder="напр. Оренда офісу"></label>
        <div class="automation-form-grid three">
          <label class="field"><span>Періодичність</span><select class="input" id="recurringFrequency">
            <option value="daily">Щодня</option><option value="weekly">Щотижня</option><option value="monthly" selected>Щомісяця</option><option value="yearly">Щороку</option>
          </select></label>
          <label class="field"><span>Інтервал</span><input class="input" id="recurringInterval" type="number" min="1" max="365" step="1" value="1" required></label>
          <label class="field"><span>Початок</span><input class="input" id="recurringStart" type="date" value="${localIsoDate()}" required></label>
        </div>
        <label class="automation-check-row">
          <input type="checkbox" id="recurringAutoCreate" checked>
          <span><strong>Створювати операції автоматично</strong><small>Ruby Finance додасть їх у день настання без дублювання.</small></span>
        </label>
        <div class="automation-status" id="recurringStatus" role="status" aria-live="polite"></div>
        <div class="automation-submit-row">
          <button type="button" class="btn btn-secondary" id="recurringCancel" hidden>Скасувати редагування</button>
          <button type="submit" class="btn btn-primary" id="recurringSave">Створити операцію</button>
        </div>
      </form>
    </div>`;
}

function normalizedCategories(full) {
  return {
    expense: full?.expense && typeof full.expense === 'object' ? full.expense : {},
    income: full?.income && typeof full.income === 'object' ? full.income : {},
  };
}

function bindRecurringForm(root, { categoriesByType, operations, suggestions, onRefresh }) {
  const form = root.querySelector('#recurringForm');
  const type = root.querySelector('#recurringType');
  const amount = root.querySelector('#recurringAmount');
  const currency = root.querySelector('#recurringCurrency');
  const source = root.querySelector('#recurringSource');
  const category = root.querySelector('#recurringCategory');
  const subcategory = root.querySelector('#recurringSubcategory');
  const description = root.querySelector('#recurringDescription');
  const frequency = root.querySelector('#recurringFrequency');
  const interval = root.querySelector('#recurringInterval');
  const start = root.querySelector('#recurringStart');
  const autoCreate = root.querySelector('#recurringAutoCreate');
  const save = root.querySelector('#recurringSave');
  const cancel = root.querySelector('#recurringCancel');
  const status = root.querySelector('#recurringStatus');
  const title = root.querySelector('#recurringFormTitle');
  let editingId = null;
  let busy = false;

  const categoriesFor = (kind) => Object.keys(categoriesByType[kind] || {});
  const syncSubcategories = (preferred = null) => {
    const values = categoriesByType[type.value]?.[category.value]?.subcategories;
    const options = Array.isArray(values) ? values : [];
    setHTML(subcategory, `<option value="">Без підрозділу</option>${categoryOptions(options, preferred)}`);
  };
  const syncCategories = (preferredCategory = null, preferredSubcategory = null) => {
    const values = categoriesFor(type.value);
    setHTML(category, categoryOptions(values, values.includes(preferredCategory) ? preferredCategory : values[0]));
    category.disabled = values.length === 0;
    save.disabled = values.length === 0;
    syncSubcategories(preferredSubcategory);
  };
  type.addEventListener('change', () => syncCategories());
  category.addEventListener('change', () => syncSubcategories());
  syncCategories();

  const resetForm = () => {
    editingId = null;
    form.reset();
    type.value = 'expense';
    currency.value = 'UAH';
    source.value = '';
    frequency.value = 'monthly';
    interval.value = '1';
    start.value = localIsoDate();
    autoCreate.checked = true;
    syncCategories();
    title.textContent = 'Регулярна операція';
    save.textContent = 'Створити операцію';
    cancel.hidden = true;
    status.textContent = '';
    status.className = 'automation-status';
  };

  const fillForm = (value) => {
    editingId = value.id || null;
    type.value = value.type;
    amount.value = String(value.amount ?? value.amountUah ?? '');
    currency.value = value.currency || 'UAH';
    source.value = value.paymentSource || '';
    syncCategories(value.category, value.subcategory);
    description.value = value.description || '';
    frequency.value = value.frequency;
    interval.value = String(value.interval || 1);
    start.value = value.startDate || value.nextDate || localIsoDate();
    autoCreate.checked = value.autoCreate !== false;
    title.textContent = editingId ? 'Редагувати регулярну операцію' : 'Перевірити знайдене повторення';
    save.textContent = editingId ? 'Зберегти зміни' : 'Створити операцію';
    cancel.hidden = false;
    status.textContent = editingId ? 'Змініть потрібні поля та збережіть.' : 'Шаблон лише заповнено — перевірте його перед створенням.';
    status.className = 'automation-status';
    form.scrollIntoView({ behavior: 'smooth', block: 'start' });
    amount.focus();
  };

  cancel.addEventListener('click', resetForm);
  root.querySelectorAll('[data-suggestion-index]').forEach((button) => {
    button.addEventListener('click', () => {
      const suggestion = suggestions[Number(button.dataset.suggestionIndex)];
      if (!suggestion) return;
      fillForm({ ...suggestion, interval: 1, autoCreate: true });
      Telegram.haptic('selection');
    });
  });
  root.querySelectorAll('[data-recurring-edit]').forEach((button) => {
    button.addEventListener('click', () => {
      const operation = operations.find((candidate) => String(candidate.id) === button.dataset.recurringEdit);
      if (!operation) return;
      fillForm(operation);
      Telegram.haptic('selection');
    });
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const numericAmount = Number(amount.value);
    const numericInterval = Number(interval.value);
    if (busy || !Number.isFinite(numericAmount) || numericAmount <= 0 || !Number.isInteger(numericInterval) || numericInterval < 1 || !category.value || !start.value) {
      status.textContent = 'Перевірте суму, категорію, інтервал і дату початку.';
      status.className = 'automation-status error';
      return;
    }
    const payload = {
      type: type.value,
      amount: numericAmount,
      currency: currency.value,
      category: category.value,
      subcategory: subcategory.value || null,
      description: description.value.trim() || category.value,
      payment_source: source.value || null,
      frequency: frequency.value,
      interval: numericInterval,
      start_date: start.value,
      auto_create: autoCreate.checked,
    };
    busy = true;
    save.disabled = true;
    save.textContent = 'Зберігаємо…';
    status.textContent = '';
    try {
      if (editingId) {
        const original = operations.find((operation) => operation.id === editingId);
        const patch = buildRecurringPatch(original, payload);
        if (!Object.keys(patch).length) {
          busy = false;
          save.disabled = false;
          save.textContent = 'Зберегти зміни';
          status.textContent = 'Змін немає.';
          return;
        }
        await Api.patchRecurringOperation(editingId, patch);
      } else {
        await Api.addRecurringOperation(payload);
      }
      Telegram.haptic('success');
      toast(editingId ? 'Регулярну операцію оновлено' : 'Регулярну операцію створено');
      await onRefresh();
    } catch (error) {
      busy = false;
      save.disabled = false;
      save.textContent = editingId ? 'Спробувати ще раз' : 'Повторити створення';
      status.textContent = error.message || 'Не вдалося зберегти операцію.';
      status.className = 'automation-status error';
      Telegram.haptic('error');
    }
  });

  root.querySelectorAll('[data-recurring-toggle]').forEach((button) => {
    button.addEventListener('click', async () => {
      const operation = operations.find((candidate) => String(candidate.id) === button.dataset.recurringToggle);
      if (!operation || button.disabled) return;
      button.disabled = true;
      try {
        await Api.patchRecurringOperation(operation.id, { active: !operation.active });
        Telegram.haptic('success');
        toast(operation.active ? 'Операцію призупинено' : 'Операцію відновлено');
        await onRefresh();
      } catch (error) {
        button.disabled = false;
        Telegram.haptic('error');
        toast(error.message || 'Не вдалося змінити стан');
      }
    });
  });

  root.querySelectorAll('[data-recurring-delete]').forEach((button) => {
    button.addEventListener('click', async () => {
      const operation = operations.find((candidate) => String(candidate.id) === button.dataset.recurringDelete);
      if (!operation || !window.confirm(`Видалити регулярну операцію «${operation.description || operation.category}»? Уже створені операції залишаться.`)) return;
      button.disabled = true;
      try {
        await Api.deleteRecurringOperation(operation.id);
        Telegram.haptic('success');
        toast('Регулярну операцію видалено');
        await onRefresh();
      } catch (error) {
        button.disabled = false;
        Telegram.haptic('error');
        toast(error.message || 'Не вдалося видалити операцію');
      }
    });
  });
}

export async function renderRecurringSettings(root, onBack) {
  const generation = ++renderGeneration;
  root.dataset.automationView = 'recurring';
  setHTML(root, loading('Регулярні операції'));
  wireBack(root, onBack);
  try {
    const [rawOperations, rawSuggestions, full] = await Promise.all([
      Api.recurringOperations(),
      Api.recurringSuggestions(),
      Api.categoriesFull(),
    ]);
    if (generation !== renderGeneration || root.dataset.automationView !== 'recurring') return;
    const operations = normalizeRecurringOperations(rawOperations);
    const suggestions = normalizeRecurringSuggestions(rawSuggestions);
    const categoriesByType = normalizedCategories(full);
    setHTML(root, `${header('Регулярні операції')}
      <div class="panel automation-explainer">
        <span class="avatar">↻</span><span><strong>Менше ручного вводу</strong><small>Автоматизуйте оренду, підписки, зарплати й регулярні надходження. Знайдені повторення ніколи не активуються без вашого підтвердження.</small></span>
      </div>
      ${suggestionCards(suggestions)}
      ${recurringForm(categoriesByType)}
      <div class="section-head"><div class="section-title">Ваші регулярні операції</div><span class="section-link">${operations.length}</span></div>
      ${recurringCards(operations)}
    `);
    wireBack(root, onBack);
    bindRecurringForm(root, {
      categoriesByType,
      operations,
      suggestions,
      onRefresh: () => renderRecurringSettings(root, onBack),
    });
  } catch (error) {
    if (generation !== renderGeneration || root.dataset.automationView !== 'recurring') return;
    setHTML(root, errorMarkup('Регулярні операції', error.message));
    wireBack(root, onBack);
    root.querySelector('.automation-retry')?.addEventListener('click', () => renderRecurringSettings(root, onBack));
  }
}

function currentWeekStart() {
  const today = new Date();
  const mondayOffset = (today.getDay() + 6) % 7;
  today.setDate(today.getDate() - mondayOffset);
  return localIsoDate(today);
}

function digestMarkup(settings, digest) {
  return `
    ${header('Недільний дайджест')}
    <div class="panel digest-optin-card">
      <div class="digest-optin-head">
        <span class="avatar" aria-hidden="true">☼</span>
        <span><strong>Підсумок тижня в Telegram</strong><small>Доходи, витрати, результат і найбільша категорія — одним повідомленням у неділю.</small></span>
      </div>
      <label class="digest-switch-row" for="weeklyDigestToggle">
        <span><strong>${settings.weeklyDigestEnabled ? 'Дайджест увімкнено' : 'Дайджест вимкнено'}</strong><small>Функція вимкнена за замовчуванням і працює лише після вашої згоди.</small></span>
        <input type="checkbox" role="switch" id="weeklyDigestToggle" ${settings.weeklyDigestEnabled ? 'checked' : ''} aria-label="Отримувати недільний дайджест">
      </label>
      <div class="automation-status" id="digestStatus" role="status" aria-live="polite"></div>
    </div>
    <div class="section-head"><div class="section-title">Попередній перегляд</div><span class="section-link">цей тиждень</span></div>
    <div class="balance-card digest-preview" style="min-height:auto;">
      <div class="balance-label">${esc(uiDate(digest.periodStart))} — ${esc(uiDate(digest.periodEnd))}</div>
      <div class="balance-value">${esc(fmtMoney(digest.net, 'UAH'))}</div>
      <div class="metric-row">
        <div class="metric"><span>Доходи</span><strong>${esc(fmtAmount(digest.totalIncome, 'UAH'))}</strong></div>
        <div class="metric"><span>Витрати</span><strong>${esc(fmtAmount(digest.totalExpense, 'UAH'))}</strong></div>
      </div>
    </div>
    <div class="panel digest-detail">
      <div><span>Операцій</span><strong>${digest.transactionCount}</strong></div>
      <div><span>Найбільша категорія витрат</span><strong>${digest.topExpenseCategory ? `${esc(digest.topExpenseCategory)} · ${esc(fmtAmount(digest.topExpenseAmount, 'UAH'))}` : 'Ще немає витрат'}</strong></div>
    </div>
    <p class="automation-disclaimer">Дані обробляються правилами Ruby Finance без AI та не надсилаються стороннім аналітичним сервісам.</p>
  `;
}

export async function renderDigestSettings(root, onBack) {
  const generation = ++renderGeneration;
  root.dataset.automationView = 'digest';
  setHTML(root, loading('Недільний дайджест'));
  wireBack(root, onBack);
  try {
    const [rawSettings, rawDigest] = await Promise.all([
      Api.notificationSettings(),
      Api.weeklyDigest(currentWeekStart()),
    ]);
    if (generation !== renderGeneration || root.dataset.automationView !== 'digest') return;
    const settings = normalizeNotificationSettings(rawSettings);
    const digest = normalizeDigest(rawDigest);
    if (!digest) throw new Error('Сервер повернув неповний попередній перегляд.');
    setHTML(root, digestMarkup(settings, digest));
    wireBack(root, onBack);
    const toggle = root.querySelector('#weeklyDigestToggle');
    const status = root.querySelector('#digestStatus');
    toggle.addEventListener('change', async () => {
      const requested = toggle.checked;
      toggle.disabled = true;
      status.textContent = 'Зберігаємо вибір…';
      status.className = 'automation-status';
      try {
        const saved = normalizeNotificationSettings(await Api.patchNotificationSettings({
          weekly_digest_enabled: requested,
        }));
        toggle.checked = saved.weeklyDigestEnabled;
        Telegram.haptic('success');
        toast(saved.weeklyDigestEnabled ? 'Недільний дайджест увімкнено' : 'Недільний дайджест вимкнено');
        await renderDigestSettings(root, onBack);
      } catch (error) {
        toggle.checked = !requested;
        toggle.disabled = false;
        status.textContent = error.message || 'Не вдалося зберегти налаштування.';
        status.className = 'automation-status error';
        Telegram.haptic('error');
      }
    });
  } catch (error) {
    if (generation !== renderGeneration || root.dataset.automationView !== 'digest') return;
    setHTML(root, errorMarkup('Недільний дайджест', error.message));
    wireBack(root, onBack);
    root.querySelector('.automation-retry')?.addEventListener('click', () => renderDigestSettings(root, onBack));
  }
}
