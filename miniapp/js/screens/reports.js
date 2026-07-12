/* Reports screen — full tab set: Огляд / Працівники / Податки / Бухгалтерія / Час / AI */

import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, fmtAmount, esc, toast, setHTML } from '../ui.js';
import { normalizeMonthlyReport } from '../report-data.js';

const SLICE_COLORS = ['#6E0F1F', '#D8B56D', '#9B1B30', '#6FB67E', '#D45A4F', '#7A6E66'];

const MONTH_NAMES = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                    'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень'];

const TABS = [
  { id: 'overview',   label: 'Огляд' },
  { id: 'employees',  label: 'Працівники' },
  { id: 'tax',        label: 'Податки' },
  { id: 'accounting', label: 'Бухгалтерія' },
  { id: 'time',       label: 'Час' },
  { id: 'ai',         label: 'AI' },
];

const state = {
  tab: 'overview',
  year: new Date().getFullYear(),
  month: new Date().getMonth() + 1,
  data: {},  // cached per-tab
  drill: null,  // {type, category} when drilled into one category's subcategories
};
let renderGeneration = 0;

// ── Helpers ─────────────────────────────────────────────────────
function donutGradient(slices) {
  if (!slices.length) return `conic-gradient(${SLICE_COLORS[5]} 0 100%)`;
  const total = slices.reduce((s, x) => s + x.value, 0) || 1;
  let acc = 0;
  return 'conic-gradient(' + slices.map((s, i) => {
    const start = (acc / total) * 100;
    acc += s.value;
    const end = (acc / total) * 100;
    return `${SLICE_COLORS[i % SLICE_COLORS.length]} ${start}% ${end}%`;
  }).join(', ') + ')';
}

function monthLabel() {
  return `${MONTH_NAMES[state.month]} ${state.year}`;
}

function emptyState(text) {
  return `<div class="empty-state" style="padding: var(--sp-4);">
    <div class="icon">∅</div>
    <p>${esc(text)}</p>
  </div>`;
}

function loadingSkeleton() {
  return `<div class="panel" style="padding: var(--sp-4);">
    <div class="sk" style="height:80px;margin-bottom:8px;"></div>
    <div class="sk" style="height:20px;width:60%;"></div>
  </div>`;
}

// ── AI prompt builder + modal (kept from prior version) ────────
function buildAIPrompt(report, label) {
  const { incomeSlices, expenseSlices, totalIncome, totalExpense } = report;
  const balance = totalIncome - totalExpense;
  let r = `🤖 АНАЛІЗ ФІНАНСІВ ДЛЯ AI

Ти фінансовий аналітик. Проаналізуй мої фінанси за ${label}.

━━━ ЗАГАЛЬНА ІНФОРМАЦІЯ ━━━
Дохід: ${totalIncome.toFixed(2)} UAH
Витрати: ${totalExpense.toFixed(2)} UAH
Баланс: ${balance >= 0 ? '+' : ''}${balance.toFixed(2)} UAH (${(totalIncome > 0 ? balance/totalIncome*100 : 0).toFixed(1)}%)

━━━ ДОХОДИ ━━━
`;
  incomeSlices.forEach(({ name: cat, value: v }, i) => {
    const pct = totalIncome > 0 ? (v / totalIncome * 100).toFixed(1) : '0.0';
    r += `${i + 1}. ${cat}: ${v.toFixed(2)} UAH (${pct}%)\n`;
  });
  r += `\n━━━ ВИТРАТИ ━━━\n`;
  expenseSlices.forEach(({ name: cat, value: v }, i) => {
    const pct = totalExpense > 0 ? (v / totalExpense * 100).toFixed(1) : '0.0';
    r += `${i + 1}. ${cat}: ${v.toFixed(2)} UAH (${pct}%)\n`;
  });
  r += `
━━━ ЗАВДАННЯ ━━━
1. Оптимізація витрат — які категорії можна скоротити?
2. Фінансові ризики — на що звернути увагу?
3. Можливості зростання доходу.
4. Поради щодо податків: врахуй мою фактичну групу та актуальні ставки; не припускай групу без моїх даних.
5. Дай 3 конкретні рекомендації на наступний місяць.`;
  return r;
}

function openAIModal(prompt) {
  let modal = document.getElementById('aiModal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'aiModal';
    modal.className = 'ai-modal';
    modal.innerHTML = `
      <div class="ai-modal-backdrop"></div>
      <div class="ai-modal-panel">
        <div class="ai-modal-head">
          <div class="ai-modal-title">🤖 AI-аналіз готовий</div>
          <button class="ai-modal-close" aria-label="Закрити">×</button>
        </div>
        <div class="ai-modal-body">
          <p class="ai-modal-hint">Скопіюйте текст і вставте у ChatGPT, Claude або Gemini.</p>
          <pre class="ai-modal-pre" id="aiPromptText"></pre>
        </div>
        <div class="ai-modal-foot">
          <button class="btn btn-secondary" id="aiClose">Закрити</button>
          <button class="btn btn-primary" id="aiCopy">📋 Скопіювати текст</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  document.getElementById('aiPromptText').textContent = prompt;
  const close = () => { modal.classList.remove('show'); Telegram.haptic('selection'); };
  modal.querySelector('.ai-modal-backdrop').onclick = close;
  modal.querySelector('.ai-modal-close').onclick = close;
  modal.querySelector('#aiClose').onclick = close;
  modal.querySelector('#aiCopy').onclick = async () => {
    try {
      await navigator.clipboard.writeText(prompt);
      Telegram.haptic('success');
      toast('Текст скопійовано');
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = prompt;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); toast('Скопійовано'); } catch (e) { toast('Не вдалося скопіювати'); }
      document.body.removeChild(ta);
    }
  };
  requestAnimationFrame(() => modal.classList.add('show'));
}

// ── Tab renderers ──────────────────────────────────────────────
function overviewMarkup(report) {
  const { expenseSlices: slices, incomeSlices, totalExpense, totalIncome } = report;
  const legend = slices.map((s, i) => {
    const pct = ((s.value / (totalExpense || 1)) * 100).toFixed(0);
    // Always allow drill-down: historical transactions can retain a
    // subcategory that was later removed from the current category settings.
    const hasSub = true;
    const drillAttr = ` data-drill="expense" data-drill-cat="${esc(s.name)}"`;
    return `
      <div class="legend-item ${hasSub ? 'drillable' : ''}"${drillAttr}>
        <span class="swatch" style="background:${SLICE_COLORS[i % SLICE_COLORS.length]}"></span>
        <span>${esc(s.name)}${hasSub ? ' <span class="drill-arrow">›</span>' : ''}</span>
        <strong>${esc(fmtAmount(s.value, 'UAH'))} <span class="legend-pct">(${pct}%)</span></strong>
      </div>`;
  }).join('');
  const incomeBars = incomeSlices.map(({ name: k, value: v }) => {
    const hasSub = true;
    const drillAttr = ` data-drill="income" data-drill-cat="${esc(k)}"`;
    return `
    <div class="${hasSub ? 'drillable-bar' : ''}"${drillAttr}>
      <div class="bar-meta"><span>${esc(k)}${hasSub ? ' <span class="drill-arrow">›</span>' : ''}</span><strong>${esc(fmtAmount(v, 'UAH'))}</strong></div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (v / (totalIncome || 1)) * 100).toFixed(0)}%"></div></div>
    </div>`;
  }).join('');
  return `
    <div class="balance-card" style="min-height:auto;">
      <div class="balance-label">${esc(monthLabel())}</div>
      <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtMoney(totalIncome - totalExpense, 'UAH'))}</div>
      <div class="metric-row">
        <div class="metric"><span>Доходи</span><strong>${esc(fmtAmount(totalIncome, 'UAH'))}</strong></div>
        <div class="metric"><span>Витрати</span><strong>${esc(fmtAmount(totalExpense, 'UAH'))}</strong></div>
      </div>
    </div>
    <div class="section-head"><div class="section-title">Витрати по категоріях</div></div>
    <div class="panel" style="padding: var(--sp-4);">
      ${slices.length ? `
        <div class="report-row">
          <div class="donut" style="background:${donutGradient(slices)}"></div>
          <div class="legend">${legend}</div>
        </div>` : emptyState('Немає витрат за цей місяць')}
    </div>
    <div class="section-head"><div class="section-title">Доходи по джерелах</div></div>
    <div class="panel" style="padding: var(--sp-4);">
      ${incomeSlices.length ? `<div class="bars">${incomeBars}</div>` : emptyState('Немає доходів за цей місяць')}
    </div>`;
}

async function renderOverview(container, generation) {
  setHTML(container, loadingSkeleton());
  try {
    const report = normalizeMonthlyReport(await Api.monthlyReport(state.year, state.month));
    if (generation !== renderGeneration) return;
    state.data.overview = report;
    setHTML(container, overviewMarkup(report));
    wireDrill(container);
  } catch (e) {
    if (generation !== renderGeneration) return;
    setHTML(container, emptyState('Помилка: ' + (e.message || 'не вдалось завантажити')));
  }
}

async function renderEmployees(container) {
  container.innerHTML = loadingSkeleton();
  try {
    const data = await Api.employeeReport(state.year, state.month);
    state.data.employees = data;
    if (!data || !data.length) {
      container.innerHTML = emptyState('Немає даних по працівниках за цей місяць');
      return;
    }
    container.innerHTML = `
      <div class="section-head"><div class="section-title">ROI працівників · ${esc(monthLabel())}</div></div>
      <div class="row-list">
        ${data.map((e) => {
          const profitColor = e.profit > 0 ? 'income' : (e.profit < 0 ? 'expense' : '');
          const roiSign = e.roi >= 0 ? '+' : '';
          return `
            <div class="panel" style="padding: var(--sp-4); margin-bottom: var(--sp-2);">
              <div class="row" style="border: 0; background: transparent; padding: 0;">
                <div class="avatar">${esc((e.name?.[0] || '?').toUpperCase())}</div>
                <div>
                  <div class="row-title">${esc(e.name)}</div>
                  <div class="row-meta">ROI ${roiSign}${e.roi.toFixed(1)}%</div>
                </div>
                <div class="amount ${profitColor}">${esc(fmtMoney(e.profit, 'UAH'))}</div>
              </div>
              <div class="bars" style="margin-top: var(--sp-3);">
                <div>
                  <div class="bar-meta"><span>Дохід</span><strong>${esc(fmtMoney(e.income, 'UAH'))}</strong></div>
                  <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, e.income / Math.max(e.income, e.salary, 1) * 100).toFixed(0)}%"></div></div>
                </div>
                <div>
                  <div class="bar-meta"><span>ЗП</span><strong>${esc(fmtMoney(e.salary, 'UAH'))}</strong></div>
                  <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, e.salary / Math.max(e.income, e.salary, 1) * 100).toFixed(0)}%; background: linear-gradient(90deg, var(--ruby-graphite), var(--ruby-danger));"></div></div>
                </div>
              </div>
            </div>`;
        }).join('')}
      </div>`;
  } catch (e) {
    container.innerHTML = emptyState('Помилка: ' + (e.message || 'не вдалось завантажити'));
  }
}

async function renderTax(container) {
  container.innerHTML = loadingSkeleton();
  try {
    const d = await Api.taxReport(state.year, state.month);
    state.data.tax = d;
    const groupLabel = d.group_label || 'ФОП 3 група';
    const isNotFop = d.group === 'none';

    let metricRow = '';
    if (isNotFop) {
      metricRow = `<div class="metric" style="grid-column: 1 / -1;"><span>Без нарахувань</span><strong>Фізособа — податки не нараховуємо</strong></div>`;
    } else {
      const singleLabel = d.group === 'fop3'
        ? `Єдиний податок (${d.scheme_label || `${(d.single_tax_rate * 100).toFixed(0)}%`})`
        : 'Єдиний податок (фіксований)';
      metricRow = `
        <div class="metric"><span>${esc(singleLabel)}</span><strong>${esc(fmtAmount(d.single_tax, 'UAH'))}</strong></div>
        <div class="metric"><span>ЄСВ (фіксований)</span><strong>${esc(fmtAmount(d.esv_fixed, 'UAH'))}</strong></div>
        <div class="metric" style="grid-column: 1 / -1;"><span>Військовий збір</span><strong>${esc(fmtAmount(d.military_levy, 'UAH'))}</strong></div>
      `;
    }

    const hintText = isNotFop
      ? 'Як фізособа ви не сплачуєте єдиний податок, ЄСВ і військовий збір у межах цього ФОП-розрахунку. Якщо ви ФОП — змініть групу у Меню → Налаштування → Податки.'
      : d.group === 'fop3'
        ? `${d.scheme_label || `${(d.single_tax_rate * 100).toFixed(0)}%`} єдиного податку + ЄСВ ${fmtAmount(d.esv_fixed, 'UAH')} + 1% військового збору від доходу.${d.vat_registered ? ' ПДВ у підсумок не включено.' : ''}`
        : `Фіксований єдиний податок ${fmtAmount(d.single_tax, 'UAH')} + ЄСВ ${fmtAmount(d.esv_fixed, 'UAH')} + військовий збір ${fmtAmount(d.military_levy, 'UAH')}.`;

    container.innerHTML = `
      <div class="balance-card" style="min-height:auto;">
        <div class="balance-label">${esc(groupLabel)} · ${esc(d.month_name)} ${d.year}</div>
        <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtAmount(d.total_tax, 'UAH'))}</div>
        <div class="metric-row">${metricRow}</div>
      </div>

      <div class="section-head"><div class="section-title">Орієнтовний розрахунок</div></div>
      <div class="panel" style="padding: var(--sp-4);">
        <div class="row-list">
          <div class="kv"><span>Період</span><strong>${esc(d.period_from)} — ${esc(d.period_to)}</strong></div>
          <div class="kv"><span>Загальний дохід</span><strong>${esc(fmtAmount(d.total_income, 'UAH'))}</strong></div>
          <div class="kv"><span>Загальні витрати</span><strong>${esc(fmtAmount(d.total_expense, 'UAH'))}</strong></div>
          <div class="kv"><span>Чистий прибуток</span><strong class="amount ${d.profit >= 0 ? 'income' : 'expense'}">${esc(fmtMoney(d.profit, 'UAH'))}</strong></div>
          <div class="kv"><span>Після податків</span><strong class="amount ${d.after_tax >= 0 ? 'income' : 'expense'}">${esc(fmtMoney(d.after_tax, 'UAH'))}</strong></div>
        </div>
      </div>

      <div class="panel ai-card" style="margin-top: var(--sp-3);">
        <div class="ai-card-row">
          <div class="ai-card-icon">📋</div>
          <div class="ai-card-text">
            <div class="ai-card-title">${esc(groupLabel)}</div>
            <div class="ai-card-sub">${esc(hintText)} Змініть параметри у Меню → Налаштування → Податки.</div>
          </div>
        </div>
      </div>
      <p class="row-meta" style="margin: var(--sp-3) var(--sp-2) 0; line-height:1.5;">${esc(d.disclaimer || 'Розрахунок інформаційний і не є податковою консультацією.')}</p>`;
  } catch (e) {
    container.innerHTML = emptyState('Помилка: ' + (e.message || 'не вдалось завантажити'));
  }
}

async function renderAccounting(container) {
  container.innerHTML = loadingSkeleton();
  try {
    const d = await Api.accountingReport(state.year, state.month);
    state.data.accounting = d;
    container.innerHTML = `
      <div class="balance-card" style="min-height:auto;">
        <div class="balance-label">Кінцеве сальдо · ${esc(monthLabel())}</div>
        <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtMoney(d.closing_balance, 'UAH'))}</div>
        <div class="metric-row">
          <div class="metric"><span>Початкове сальдо</span><strong>${esc(fmtMoney(d.opening_balance, 'UAH'))}</strong></div>
          <div class="metric"><span>Прибуток/збиток місяця</span><strong class="amount ${d.profit >= 0 ? 'income' : 'expense'}">${esc(fmtMoney(d.profit, 'UAH'))}</strong></div>
        </div>
      </div>

      <div class="section-head"><div class="section-title">Дебет-кредит проводки</div></div>
      <div class="row-list">
        ${d.entries.map((e) => `
          <div class="row">
            <div class="avatar">${esc(e.debit.split(' ')[1] || '?')}</div>
            <div>
              <div class="row-title">${esc(e.label)}</div>
              <div class="row-meta">${esc(e.debit)} → ${esc(e.credit)}</div>
            </div>
            <div class="amount">${esc(fmtMoney(e.amount, 'UAH'))}</div>
          </div>`).join('')}
      </div>

      <div class="section-head"><div class="section-title">Результат</div></div>
      <div class="panel" style="padding: var(--sp-4);">
        <div class="row" style="border: 0; background: transparent; padding: 0;">
          <div class="avatar">${d.result === 'profit' ? '✓' : '×'}</div>
          <div>
            <div class="row-title">${d.result === 'profit' ? 'Прибуток' : 'Збиток'}</div>
            <div class="row-meta">за ${esc(monthLabel())}</div>
          </div>
          <div class="amount ${d.profit >= 0 ? 'income' : 'expense'}">${esc(fmtMoney(Math.abs(d.profit), 'UAH'))}</div>
        </div>
      </div>`;
  } catch (e) {
    container.innerHTML = emptyState('Помилка: ' + (e.message || 'не вдалось завантажити'));
  }
}

async function renderTime(container) {
  container.innerHTML = loadingSkeleton();
  try {
    const d = await Api.timeReport(state.year, state.month);
    state.data.time = d;
    if (!d || !d.total_minutes) {
      container.innerHTML = emptyState('Немає записів часу за цей місяць');
      return;
    }
    container.innerHTML = `
      <div class="balance-card" style="min-height:auto;">
        <div class="balance-label">Усього часу · ${esc(monthLabel())}</div>
        <div class="balance-value" style="font-size: var(--fs-32);">${d.total_hours.toFixed(1)} год</div>
        <div class="metric-row">
          <div class="metric"><span>Днів у місяці</span><strong>${d.days_in_month}</strong></div>
          <div class="metric"><span>Середньо/день</span><strong>${d.avg_per_day_hours.toFixed(1)} год</strong></div>
        </div>
      </div>

      <div class="section-head"><div class="section-title">Топ категорій</div></div>
      <div class="panel" style="padding: var(--sp-4);">
        <div class="bars">
          ${d.by_category.slice(0, 8).map((c) => `
            <div>
              <div class="bar-meta">
                <span>${esc(c.emoji || '⏱')} ${esc(c.name)}</span>
                <strong>${c.hours.toFixed(1)} год · ${c.percentage.toFixed(0)}%</strong>
              </div>
              <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, c.percentage).toFixed(0)}%"></div></div>
            </div>`).join('')}
        </div>
      </div>

      <div class="section-head"><div class="section-title">Продуктивність</div></div>
      <div class="row-list">
        <div class="row">
          <div class="avatar" style="background: linear-gradient(145deg, rgba(111,182,126,.25), rgba(111,182,126,.10));">🟢</div>
          <div><div class="row-title">Корисний час</div><div class="row-meta">робота, навчання, спорт</div></div>
          <div class="amount income">${(d.productive_minutes / 60).toFixed(1)} год</div>
        </div>
        <div class="row">
          <div class="avatar" style="background: linear-gradient(145deg, rgba(232,184,99,.25), rgba(232,184,99,.10));">🟡</div>
          <div><div class="row-title">Непродуктивний</div><div class="row-meta">розваги, скрол</div></div>
          <div class="amount">${(d.unproductive_minutes / 60).toFixed(1)} год</div>
        </div>
        <div class="row">
          <div class="avatar">🔵</div>
          <div><div class="row-title">Відпочинок</div><div class="row-meta">сон, їжа, відпустка</div></div>
          <div class="amount">${(d.rest_minutes / 60).toFixed(1)} год</div>
        </div>
        <div class="row">
          <div class="avatar">∅</div>
          <div><div class="row-title">Невідстежено</div><div class="row-meta">сліпі зони</div></div>
          <div class="amount expense">${(d.untracked_minutes / 60).toFixed(1)} год</div>
        </div>
      </div>`;
  } catch (e) {
    container.innerHTML = emptyState('Помилка: ' + (e.message || 'не вдалось завантажити'));
  }
}

async function renderAI(container, generation) {
  setHTML(container, loadingSkeleton());
  try {
    const report = normalizeMonthlyReport(await Api.monthlyReport(state.year, state.month));
    if (generation !== renderGeneration) return;
    setHTML(container, `
    <div class="panel ai-card">
      <div class="ai-card-row">
        <div class="ai-card-icon">🤖</div>
        <div class="ai-card-text">
          <div class="ai-card-title">Готовий промпт для ChatGPT / Claude / Gemini</div>
          <div class="ai-card-sub">Згенерую структурований аналіз ваших фінансів за ${esc(monthLabel())} — скопіюйте і вставте у AI-чат.</div>
        </div>
      </div>
      <button class="btn btn-primary" id="genAIBtn" style="margin-top: var(--sp-3);">
        🤖 Згенерувати AI-аналіз
      </button>
    </div>
    <div class="panel ai-card" style="margin-top: var(--sp-3);">
      <div class="ai-card-row">
        <div class="ai-card-icon">🔒</div>
        <div class="ai-card-text">
          <div class="ai-card-title">Все на пристрої</div>
          <div class="ai-card-sub">Промпт генерується тут, не передається на жодний AI-сервіс автоматично. Ви самі вирішуєте, куди вставляти.</div>
        </div>
      </div>
    </div>`);
  container.querySelector('#genAIBtn').addEventListener('click', () => {
    Telegram.haptic('medium');
    openAIModal(buildAIPrompt(report, monthLabel()));
  });
  } catch (e) {
    if (generation !== renderGeneration) return;
    setHTML(container, emptyState('Помилка: ' + (e.message || 'не вдалось завантажити')));
  }
}

// Drill into one category → donut by subcategory
async function renderCategoryDrill(container, drill) {
  setHTML(container, loadingSkeleton());
  try {
    const d = await Api.categoryBreakdown(drill.type, drill.category, 'month', state.year, state.month);
    const slices = (d.breakdown || []).map((b) => ({ name: b.name, value: b.value }));
    const legend = slices.map((s, i) => {
      const pct = ((s.value / (d.total || 1)) * 100).toFixed(0);
      return `
        <div class="legend-item">
          <span class="swatch" style="background:${SLICE_COLORS[i % SLICE_COLORS.length]}"></span>
          <span>${esc(s.name)}</span>
          <strong>${esc(fmtAmount(s.value, 'UAH'))} <span class="legend-pct">(${pct}%)</span></strong>
        </div>`;
    }).join('');

    setHTML(container, `
      <button class="btn btn-secondary drill-back" id="drillBack" style="margin-bottom: var(--sp-3);">‹ Назад до категорій</button>
      <div class="balance-card" style="min-height:auto;">
        <div class="balance-label">${esc(drill.category)} · ${esc(monthLabel())}</div>
        <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtAmount(d.total, 'UAH'))}</div>
        <div class="balance-sub">${drill.type === 'expense' ? 'витрати' : 'доходи'} по підрозділах</div>
      </div>
      <div class="section-head"><div class="section-title">Розбивка по підрозділах</div></div>
      <div class="panel" style="padding: var(--sp-4);">
        ${slices.length ? `
          <div class="report-row">
            <div class="donut" style="background:${donutGradient(slices)}"></div>
            <div class="legend">${legend}</div>
          </div>` : emptyState('Немає операцій у цій категорії за період')}
      </div>`);

    container.querySelector('#drillBack')?.addEventListener('click', () => {
      state.drill = null;
      Telegram.haptic('selection');
      renderReports();
    });
  } catch (e) {
    setHTML(container, `
      <button class="btn btn-secondary drill-back" id="drillBack" style="margin-bottom: var(--sp-3);">‹ Назад</button>
      ${emptyState('Помилка: ' + (e.message || 'не вдалось завантажити'))}`);
    container.querySelector('#drillBack')?.addEventListener('click', () => {
      state.drill = null;
      renderReports();
    });
  }
}

// ── Main entry ─────────────────────────────────────────────────
export function renderReports() {
  const generation = ++renderGeneration;
  const root = document.getElementById('screen-reports');
  if (!root) return;

  // Build outer chrome with tabs + month picker
  root.innerHTML = `
    <div class="month-picker">
      <button class="ghost-btn" id="prevMonth" aria-label="Попередній">‹</button>
      <div class="month-label">${esc(monthLabel())}</div>
      <button class="ghost-btn" id="nextMonth" aria-label="Наступний">›</button>
    </div>
    <div class="tab-strip" id="tabStrip">
      ${TABS.map((t) => `
        <button class="tab ${state.tab === t.id ? 'active' : ''}" data-tab="${t.id}">${esc(t.label)}</button>
      `).join('')}
    </div>
    <div id="tab-content"></div>
  `;

  // Wire month picker
  root.querySelector('#prevMonth').addEventListener('click', () => {
    state.month -= 1;
    if (state.month < 1) { state.month = 12; state.year -= 1; }
    state.drill = null;
    Telegram.haptic('selection');
    renderReports();
  });
  root.querySelector('#nextMonth').addEventListener('click', () => {
    state.month += 1;
    if (state.month > 12) { state.month = 1; state.year += 1; }
    state.drill = null;
    Telegram.haptic('selection');
    renderReports();
  });

  // Wire tabs
  root.querySelectorAll('[data-tab]').forEach((b) => {
    b.addEventListener('click', () => {
      state.tab = b.dataset.tab;
      state.drill = null;
      Telegram.haptic('selection');
      renderReports();
    });
  });

  // Render the active tab
  const content = root.querySelector('#tab-content');
  switch (state.tab) {
    case 'overview':
      if (state.drill) {
        renderCategoryDrill(content, state.drill);
      } else {
        renderOverview(content, generation);
      }
      break;
    case 'employees':  renderEmployees(content); break;
    case 'tax':        renderTax(content); break;
    case 'accounting': renderAccounting(content); break;
    case 'time':       renderTime(content); break;
    case 'ai':         renderAI(content, generation); break;
  }
}

// Wire tap-to-drill on overview legend items / income bars that have subcategories
function wireDrill(content) {
  content.querySelectorAll('[data-drill-cat]').forEach((el) => {
    el.addEventListener('click', () => {
      state.drill = { type: el.dataset.drill, category: el.dataset.drillCat };
      Telegram.haptic('selection');
      renderReports();
    });
  });
}
