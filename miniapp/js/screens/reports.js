/* Reports screen — full tab set: Огляд / Працівники / Податки / Бухгалтерія / Час / AI */

import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, fmtAmount, esc, toast, setHTML } from '../ui.js';
import { normalizeMonthlyReport } from '../report-data.js';
import {
  budgetTone,
  normalizeBudgetResponse,
  normalizePaymentSourceBreakdown,
} from '../block2-ui.js';
import { normalizeForecast } from '../automation-ui.js';

const SLICE_COLORS = ['#6E0F1F', '#D8B56D', '#9B1B30', '#6FB67E', '#D45A4F', '#7A6E66'];

const MONTH_NAMES = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                    'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень'];

const TABS = [
  { id: 'overview',   label: 'Огляд' },
  { id: 'employees',  label: 'Команда' },
  { id: 'tax',        label: 'Податки' },
  { id: 'accounting', label: 'Облік' },
  { id: 'time',       label: 'Час' },
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
function forecastMarkup(state) {
  if (state?.data) {
    const forecast = state.data;
    return `
      <div class="section-head"><div class="section-title">Прогноз результату місяця</div><span class="section-link">записано + заплановано</span></div>
      <div class="panel forecast-card">
        <div class="forecast-kicker">Орієнтовний результат після податків</div>
        <div class="forecast-value">${esc(fmtMoney(forecast.projectedAfterTax, 'UAH'))}</div>
        <div class="forecast-grid">
          <div><span>Записаний результат</span><strong>${esc(fmtMoney(forecast.currentNet, 'UAH'))}</strong></div>
          <div><span>Заплановані доходи</span><strong>${esc(fmtAmount(forecast.scheduledIncome, 'UAH'))}</strong></div>
          <div><span>Заплановані витрати</span><strong>${esc(fmtAmount(forecast.scheduledExpense, 'UAH'))}</strong></div>
          <div><span>Орієнтовні податки</span><strong>${esc(fmtAmount(forecast.estimatedTax, 'UAH'))}</strong></div>
        </div>
        <p>Це прогноз результату за обраний місяць, а не баланс банківського рахунку.</p>
      </div>`;
  }
  return `
    <div class="section-head"><div class="section-title">Прогноз результату місяця</div></div>
    <div class="panel forecast-error" role="status">
      <span>Не вдалося завантажити прогноз.</span>
      <button type="button" class="btn btn-secondary forecast-retry">Повторити</button>
    </div>`;
}

// ── Money-flow (Sankey) ────────────────────────────────────────
// A curved band between two vertical segments (source [ya0,ya1] → dest [yb0,yb1]).
function flowBand(x0, ya0, ya1, x1, yb0, yb1, fill, op) {
  const mx = (x0 + x1) / 2;
  return `<path d="M ${x0} ${ya0} C ${mx} ${ya0}, ${mx} ${yb0}, ${x1} ${yb0} `
    + `L ${x1} ${yb1} C ${mx} ${yb1}, ${mx} ${ya1}, ${x0} ${ya1} Z" fill="${fill}" opacity="${op}"/>`;
}

// Honest money flow: income sources → central pool → expense categories (+ savings
// / deficit to balance). We don't know which income funded which expense, so the
// pool in the middle is the truthful model (no fabricated source→category links).
function flowChartSVG(income, expense, totalIncome, totalExpense) {
  const cap = (arr, n = 4) => {
    const clean = (arr || []).filter((x) => x.value > 0);
    if (clean.length <= n + 1) return clean;
    const rest = clean.slice(n).reduce((s, x) => s + x.value, 0);
    return [...clean.slice(0, n), { name: 'Інше', value: rest }];
  };
  const savings = totalIncome - totalExpense;
  const left = cap(income).map((x, i) => ({ ...x, color: SLICE_COLORS[i % SLICE_COLORS.length] }));
  if (savings < 0) left.push({ name: 'З резервів', value: -savings, color: '#D45A4F' });
  const right = cap(expense).map((x, i) => ({ ...x, color: SLICE_COLORS[i % SLICE_COLORS.length] }));
  if (savings > 0) right.push({ name: 'Заощадження', value: savings, color: '#6FB67E' });
  if (!left.length || !right.length) return '';

  const total = Math.max(
    left.reduce((s, x) => s + x.value, 0),
    right.reduce((s, x) => s + x.value, 0),
    1,
  );
  const W = 320, H = 250, pad = 6, usable = H - 2 * pad;
  const scale = usable / total;
  const colW = 11, leftX = 74, hubX = W / 2 - colW / 2, rightX = W - 74 - colW;
  const clip = (s, n = 12) => (String(s).length > n ? String(s).slice(0, n - 1) + '…' : String(s));
  const stack = (nodes) => {
    let y = pad; const out = [];
    for (const node of nodes) {
      const h = Math.max(2, node.value * scale);
      out.push({ node, y0: y, y1: y + h }); y += h;
    }
    return out;
  };
  const parts = [`<rect x="${hubX}" y="${pad}" width="${colW}" height="${usable}" rx="3" fill="#3a2a2e"/>`];
  stack(left).forEach(({ node, y0, y1 }) => {
    parts.push(flowBand(leftX + colW, y0, y1, hubX, y0, y1, node.color, 0.5));
    parts.push(`<rect x="${leftX}" y="${y0}" width="${colW}" height="${y1 - y0}" rx="2" fill="${node.color}"/>`);
    parts.push(`<text x="${leftX - 5}" y="${(y0 + y1) / 2 + 3}" text-anchor="end" font-size="9" fill="#CFC3B4">${esc(clip(node.name))}</text>`);
  });
  stack(right).forEach(({ node, y0, y1 }) => {
    parts.push(flowBand(hubX + colW, y0, y1, rightX, y0, y1, node.color, 0.5));
    parts.push(`<rect x="${rightX}" y="${y0}" width="${colW}" height="${y1 - y0}" rx="2" fill="${node.color}"/>`);
    parts.push(`<text x="${rightX + colW + 5}" y="${(y0 + y1) / 2 + 3}" text-anchor="start" font-size="9" fill="#CFC3B4">${esc(clip(node.name))}</text>`);
  });
  return `<svg viewBox="0 0 ${W} ${H}" class="flow-svg" role="img" aria-label="Потік коштів: доходи у витрати">${parts.join('')}</svg>`;
}

function flowSection(report) {
  const svg = flowChartSVG(report.incomeSlices, report.expenseSlices, report.totalIncome, report.totalExpense);
  if (!svg) return '';
  return `
    <div class="section-head"><div class="section-title">Потік коштів</div><span class="section-link">дохід → витрати</span></div>
    <div class="panel flow-panel">${svg}</div>`;
}

// ── Pace forecast (spending-rate extrapolation for the ongoing month) ──
function paceForecastMarkup(report, year, month) {
  const now = new Date();
  if (year !== now.getFullYear() || month !== now.getMonth() + 1) return '';
  if (!(report.totalExpense > 0)) return '';
  const daysInMonth = new Date(year, month, 0).getDate();
  const dayNow = Math.max(1, Math.min(now.getDate(), daysInMonth));
  const pct = Math.round((dayNow / daysInMonth) * 100);
  const projExpense = (report.totalExpense / dayNow) * daysInMonth;
  const remaining = Math.max(0, projExpense - report.totalExpense);
  return `
    <div class="section-head"><div class="section-title">Темп витрат</div><span class="section-link">день ${dayNow}/${daysInMonth}</span></div>
    <div class="panel pace-card">
      <div class="pace-head"><span>Прогноз витрат на кінець місяця</span><strong>${esc(fmtAmount(projExpense, 'UAH'))}</strong></div>
      <div class="pace-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>
      <p>За поточним темпом лишилось ще ~${esc(fmtAmount(remaining, 'UAH'))} до кінця місяця (минуло ${pct}% місяця).</p>
    </div>`;
}

function overviewMarkup(report, sourceSummary = {}, budgets = [], forecastState = null) {
  const { expenseSlices: slices, incomeSlices, totalExpense, totalIncome } = report;
  const paymentSources = normalizePaymentSourceBreakdown(sourceSummary, 'expense');
  const paymentSourceTotal = paymentSources.reduce((sum, source) => sum + source.value, 0);
  const legend = slices.map((s, i) => {
    const pct = ((s.value / (totalExpense || 1)) * 100).toFixed(0);
    // Always allow drill-down: historical transactions can retain a
    // subcategory that was later removed from the current category settings.
    const amount = fmtAmount(s.value, 'UAH');
    return `
      <button type="button" class="legend-item drillable drill-row" data-drill="expense" data-drill-cat="${esc(s.name)}" aria-label="${esc(`Деталізувати витрати: ${s.name}, ${amount}`)}">
        <span class="swatch" style="background:${SLICE_COLORS[i % SLICE_COLORS.length]}"></span>
        <span class="legend-name">${esc(s.name)}</span>
        <strong>${esc(amount)} <span class="legend-pct">(${pct}%)</span></strong>
        <span class="drill-chevron" aria-hidden="true">›</span>
      </button>`;
  }).join('');
  const incomeBars = incomeSlices.map(({ name: k, value: v }) => {
    const amount = fmtAmount(v, 'UAH');
    return `
    <button type="button" class="drillable-bar drill-row" data-drill="income" data-drill-cat="${esc(k)}" aria-label="${esc(`Деталізувати дохід: ${k}, ${amount}`)}">
      <div class="bar-meta"><span class="bar-name">${esc(k)}</span><strong>${esc(amount)}</strong><span class="drill-chevron" aria-hidden="true">›</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (v / (totalIncome || 1)) * 100).toFixed(0)}%"></div></div>
    </button>`;
  }).join('');
  const paymentSourceBars = paymentSources.map(({ label, value }) => `
    <div class="payment-report-row">
      <div class="bar-meta"><span class="bar-name">${esc(label)}</span><strong>${esc(fmtAmount(value, 'UAH'))}</strong></div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, value / (paymentSourceTotal || 1) * 100).toFixed(0)}%"></div></div>
    </div>
  `).join('');
  const budgetCards = budgets.map((budget) => `
    <div class="budget-report-card ${budgetTone(budget)}">
      <div class="budget-progress-head">
        <strong>${esc(budget.category)}</strong>
        <span>${esc(fmtAmount(budget.spent, 'UAH'))} / ${esc(fmtAmount(budget.monthlyLimit, 'UAH'))}</span>
      </div>
      <div class="budget-track" role="progressbar" aria-label="${esc(`Бюджет ${budget.category}`)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(100, Math.round(budget.progressPercent))}">
        <span style="width:${Math.min(100, budget.progressPercent).toFixed(1)}%"></span>
      </div>
      <div class="budget-report-meta"><span>${budget.progressPercent.toFixed(0)}%</span><span>${budget.isExceeded ? `Перевитрата ${esc(fmtAmount(Math.abs(budget.remaining), 'UAH'))}` : `Залишилось ${esc(fmtAmount(Math.max(0, budget.remaining), 'UAH'))}`}</span></div>
    </div>
  `).join('');
  return `
    <div class="balance-card" style="min-height:auto;">
      <div class="balance-label">${esc(monthLabel())}</div>
      <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtMoney(totalIncome - totalExpense, 'UAH'))}</div>
      <div class="metric-row">
        <div class="metric"><span>Доходи</span><strong>${esc(fmtAmount(totalIncome, 'UAH'))}</strong></div>
        <div class="metric"><span>Витрати</span><strong>${esc(fmtAmount(totalExpense, 'UAH'))}</strong></div>
      </div>
    </div>
    ${forecastMarkup(forecastState)}
    ${paceForecastMarkup(report, state.year, state.month)}
    ${flowSection(report)}
    <div class="section-head"><div class="section-title">Витрати по категоріях</div></div>
    ${slices.length ? '<p class="drill-hint">Оберіть категорію, щоб побачити підрозділи ›</p>' : ''}
    <div class="panel" style="padding: var(--sp-4);">
      ${slices.length ? `
        <div class="report-row">
          <div class="donut" style="background:${donutGradient(slices)}"></div>
          <div class="legend">${legend}</div>
        </div>` : emptyState('Немає витрат за цей місяць')}
    </div>
    <div class="section-head"><div class="section-title">Доходи по джерелах</div></div>
    ${incomeSlices.length ? '<p class="drill-hint">Оберіть джерело, щоб побачити деталі ›</p>' : ''}
    <div class="panel" style="padding: var(--sp-4);">
      ${incomeSlices.length ? `<div class="bars">${incomeBars}</div>` : emptyState('Немає доходів за цей місяць')}
    </div>
    <div class="section-head"><div class="section-title">Спосіб оплати витрат</div></div>
    <div class="panel" style="padding: var(--sp-4);">
      ${paymentSourceBars ? `<div class="bars">${paymentSourceBars}</div>` : emptyState('Немає витрат за цей місяць')}
    </div>
    <div class="section-head"><div class="section-title">Бюджети категорій</div></div>
    ${budgetCards
      ? `<div class="panel budget-report-list">${budgetCards}</div>`
      : `<button type="button" class="panel budget-empty-cta" data-go="settings" data-section="budgets"><span class="avatar">◎</span><span><strong>Налаштувати бюджети</strong><small>Ліміти та попередження про перевитрати</small></span><span aria-hidden="true">›</span></button>`}
    `;
}

async function renderOverview(container, generation) {
  setHTML(container, loadingSkeleton());
  try {
    const [rawReport, rawBudgets, rawForecast] = await Promise.all([
      Api.monthlyReport(state.year, state.month),
      Api.budgets(state.year, state.month).catch(() => ({ budgets: [] })),
      Api.forecast(state.year, state.month).catch((error) => ({ __error: error })),
    ]);
    const report = normalizeMonthlyReport(rawReport);
    const sourceSummary = rawReport;
    const budgets = normalizeBudgetResponse(rawBudgets)
      .filter((budget) => budget.type === 'expense');
    const forecast = rawForecast?.__error ? null : normalizeForecast(rawForecast);
    const forecastState = forecast ? { data: forecast } : { error: true };
    if (generation !== renderGeneration) return;
    state.data.overview = report;
    state.data.budgets = budgets;
    setHTML(container, overviewMarkup(report, sourceSummary, budgets, forecastState));
    wireDrill(container);
    container.querySelector('.forecast-retry')?.addEventListener('click', () => renderReports());
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
          const roiLabel = e.roi == null
            ? 'ROI —'
            : `ROI ${e.roi >= 0 ? '+' : ''}${Number(e.roi).toFixed(1)}%`;
          return `
            <div class="panel" style="padding: var(--sp-4); margin-bottom: var(--sp-2);">
              <div class="row" style="border: 0; background: transparent; padding: 0;">
                <div class="avatar">${esc((e.name?.[0] || '?').toUpperCase())}</div>
                <div>
                  <div class="row-title">${esc(e.name)}</div>
                  <div class="row-meta">${roiLabel}</div>
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
            <div class="avatar">${esc(e.debit || '?')}</div>
            <div>
              <div class="row-title">${esc(e.label)}</div>
              <div class="row-meta">Дт ${esc(e.debit)} → Кт ${esc(e.credit)} · ${esc(e.source_label || 'Не класифіковано')}</div>
            </div>
            <div class="amount">${esc(fmtMoney(e.amount, 'UAH'))}</div>
          </div>`).join('')}
      </div>
      <p class="row-meta" style="margin: var(--sp-3) var(--sp-2) 0; line-height:1.5;">${esc(d.disclaimer || '')}</p>

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
export function renderReports(focusTabId = null) {
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
    <div class="reports-tab-shell" id="reportsTabShell">
      <div class="tab-strip" id="tabStrip" role="tablist" aria-label="Розділи звіту">
        ${TABS.map((t) => `
          <button
            type="button"
            class="tab ${state.tab === t.id ? 'active' : ''}"
            id="report-tab-${t.id}"
            data-tab="${t.id}"
            role="tab"
            aria-selected="${state.tab === t.id}"
            aria-controls="tab-content"
            tabindex="${state.tab === t.id || (state.tab === 'ai' && t.id === 'overview') ? '0' : '-1'}"
          >${esc(t.label)}</button>
        `).join('')}
      </div>
    </div>
    <button
      type="button"
      class="report-ai-action ${state.tab === 'ai' ? 'active' : ''}"
      id="reportAiAction"
      data-tab="ai"
      aria-pressed="${state.tab === 'ai'}"
    >
      <span>🤖 AI-аналіз місяця</span>
      <span class="report-ai-chevron" aria-hidden="true">›</span>
    </button>
    <div
      id="tab-content"
      role="tabpanel"
      tabindex="0"
      aria-live="polite"
      aria-labelledby="${state.tab === 'ai' ? 'reportAiAction' : `report-tab-${state.tab}`}"
    ></div>
  `;

  wireTabEdgeFade(root);
  wireTabKeyboard(root);

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
    b.addEventListener('click', (event) => {
      state.tab = b.dataset.tab;
      state.drill = null;
      Telegram.haptic('selection');
      renderReports(event.detail === 0 ? b.dataset.tab : null);
    });
  });

  if (focusTabId) {
    root.querySelector(`[data-tab="${focusTabId}"]`)?.focus();
  }

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

function wireTabEdgeFade(root) {
  const shell = root.querySelector('#reportsTabShell');
  const strip = root.querySelector('#tabStrip');
  if (!shell || !strip) return;

  const update = () => {
    shell.classList.toggle('at-start', strip.scrollLeft <= 2);
    shell.classList.toggle(
      'at-end',
      strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 2,
    );
  };

  strip.addEventListener('scroll', update, { passive: true });
  requestAnimationFrame(update);
}

function wireTabKeyboard(root) {
  const tabs = [...root.querySelectorAll('[role="tab"]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('keydown', (event) => {
      let targetIndex = null;
      if (event.key === 'ArrowRight') targetIndex = (index + 1) % tabs.length;
      if (event.key === 'ArrowLeft') targetIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === 'Home') targetIndex = 0;
      if (event.key === 'End') targetIndex = tabs.length - 1;
      if (targetIndex === null) return;

      event.preventDefault();
      tabs[targetIndex].focus();
      tabs[targetIndex].click();
    });
  });
}
