/* Reports screen */

import { Store } from '../app.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, esc, toast } from '../ui.js';

const SLICE_COLORS = ['#6E0F1F', '#D8B56D', '#9B1B30', '#6FB67E', '#D45A4F', '#7A6E66'];

const MONTH_NAMES = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень',
                    'Липень', 'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень'];

function buildAIPrompt(monthTxs, monthLabel) {
  const income = {};
  const expense = {};
  let totalIncome = 0;
  let totalExpense = 0;
  for (const t of monthTxs) {
    const v = t.amount_uah || t.amount || 0;
    if (t.type === 'income') {
      totalIncome += v;
      income[t.category] = (income[t.category] || 0) + v;
    } else {
      totalExpense += v;
      expense[t.category] = (expense[t.category] || 0) + v;
    }
  }
  const balance = totalIncome - totalExpense;
  const sortDesc = (obj) => Object.entries(obj).sort((a, b) => b[1] - a[1]);

  let report = `🤖 АНАЛІЗ ФІНАНСІВ ДЛЯ AI

Ти фінансовий аналітик. Проаналізуй мої фінанси за ${monthLabel}.

━━━ ЗАГАЛЬНА ІНФОРМАЦІЯ ━━━
Дохід: ${totalIncome.toFixed(2)} UAH
Витрати: ${totalExpense.toFixed(2)} UAH
Баланс: ${balance >= 0 ? '+' : ''}${balance.toFixed(2)} UAH (${(totalIncome > 0 ? balance / totalIncome * 100 : 0).toFixed(1)}%)

━━━ ДОХОДИ ━━━
`;
  sortDesc(income).forEach(([cat, v], i) => {
    const pct = totalIncome > 0 ? (v / totalIncome * 100).toFixed(1) : '0.0';
    report += `${i + 1}. ${cat}: ${v.toFixed(2)} UAH (${pct}%)\n`;
  });
  report += `\n━━━ ВИТРАТИ ━━━\n`;
  sortDesc(expense).forEach(([cat, v], i) => {
    const pct = totalExpense > 0 ? (v / totalExpense * 100).toFixed(1) : '0.0';
    report += `${i + 1}. ${cat}: ${v.toFixed(2)} UAH (${pct}%)\n`;
  });
  report += `
━━━ ЗАВДАННЯ ━━━
1. Оптимізація витрат — які категорії можна скоротити без втрати якості життя?
2. Фінансові ризики — на що звернути увагу?
3. Можливості зростання доходу — діверсифікація джерел.
4. Поради по податках (ФОП 3 група: 5% + ЄСВ 1760 ₴).
5. Дай 3 конкретні рекомендації на наступний місяць.`;
  return report;
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
          <p class="ai-modal-hint">Скопіюйте текст і вставте у ChatGPT, Claude або Gemini — отримаєте персональний аналіз ваших фінансів.</p>
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

  const close = () => {
    modal.classList.remove('show');
    Telegram.haptic('selection');
  };
  modal.querySelector('.ai-modal-backdrop').onclick = close;
  modal.querySelector('.ai-modal-close').onclick = close;
  modal.querySelector('#aiClose').onclick = close;
  modal.querySelector('#aiCopy').onclick = async () => {
    try {
      await navigator.clipboard.writeText(prompt);
      Telegram.haptic('success');
      toast('Текст скопійовано в буфер обміну');
    } catch (_) {
      // Fallback for older WebView
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

export function renderReports() {
  const root = document.getElementById('screen-reports');
  if (!root) return;

  const txs = Store.transactions || [];
  const month = new Date();
  const monthTxs = txs.filter((t) => {
    const d = new Date(t.date);
    return d.getMonth() === month.getMonth() && d.getFullYear() === month.getFullYear();
  });

  const expenseByCat = {};
  const incomeByCat = {};
  let totalExpense = 0, totalIncome = 0;
  for (const t of monthTxs) {
    const v = t.amount_uah || t.amount || 0;
    if (t.type === 'expense') {
      totalExpense += v;
      expenseByCat[t.category] = (expenseByCat[t.category] || 0) + v;
    } else {
      totalIncome += v;
      incomeByCat[t.category] = (incomeByCat[t.category] || 0) + v;
    }
  }

  const slices = Object.entries(expenseByCat)
    .map(([k, v]) => ({ name: k, value: v }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 6);

  const legendHtml = slices.map((s, i) => `
    <div class="legend-item">
      <span class="swatch" style="background:${SLICE_COLORS[i % SLICE_COLORS.length]}"></span>
      <span>${esc(s.name)}</span>
      <strong>${((s.value / (totalExpense || 1)) * 100).toFixed(0)}%</strong>
    </div>
  `).join('');

  const incomeTopHtml = Object.entries(incomeByCat).sort((a,b) => b[1]-a[1]).slice(0, 5).map(([k, v]) => `
    <div>
      <div class="bar-meta"><span>${esc(k)}</span><strong>${esc(fmtMoney(v, 'UAH'))}</strong></div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100, (v/(totalIncome||1))*100).toFixed(0)}%"></div></div>
    </div>
  `).join('');

  const monthLabel = month.toLocaleDateString('uk-UA', { month: 'long', year: 'numeric' });

  root.innerHTML = `
    <div class="balance-card" style="min-height:auto;">
      <div class="balance-label">${esc(monthLabel)}</div>
      <div class="balance-value" style="font-size: var(--fs-32);">${esc(fmtMoney(totalIncome - totalExpense, 'UAH'))}</div>
      <div class="metric-row">
        <div class="metric"><span>Доходи</span><strong>${esc(fmtMoney(totalIncome, 'UAH'))}</strong></div>
        <div class="metric"><span>Витрати</span><strong>${esc(fmtMoney(totalExpense, 'UAH'))}</strong></div>
      </div>
    </div>

    <div class="section-head"><div class="section-title">Витрати по категоріях</div></div>
    <div class="panel" style="padding: var(--sp-4);">
      ${slices.length ? `
        <div class="report-row">
          <div class="donut" style="background:${donutGradient(slices)}"></div>
          <div class="legend">${legendHtml}</div>
        </div>
      ` : `
        <div class="empty-state" style="padding: var(--sp-4);">
          <div class="icon">∅</div>
          <p>Немає витрат за цей місяць</p>
        </div>
      `}
    </div>

    <div class="section-head"><div class="section-title">Доходи по джерелах</div></div>
    <div class="panel" style="padding: var(--sp-4);">
      ${Object.keys(incomeByCat).length ? `<div class="bars">${incomeTopHtml}</div>` : `
        <div class="empty-state" style="padding: var(--sp-4);">
          <div class="icon">∅</div>
          <p>Немає доходів за цей місяць</p>
        </div>
      `}
    </div>

    <div class="section-head"><div class="section-title">AI-аналіз</div></div>
    <div class="panel ai-card" id="aiCard">
      <div class="ai-card-row">
        <div class="ai-card-icon">🤖</div>
        <div class="ai-card-text">
          <div class="ai-card-title">Готовий промпт для ChatGPT / Claude / Gemini</div>
          <div class="ai-card-sub">Згенерую структурований аналіз ваших фінансів за ${esc(monthLabel)} — скопіюйте і вставте у AI-чат.</div>
        </div>
      </div>
      <button class="btn btn-primary" id="genAIBtn" style="margin-top: var(--sp-3);">
        🤖 Згенерувати AI-аналіз
      </button>
    </div>
  `;

  root.querySelector('#genAIBtn')?.addEventListener('click', () => {
    const prompt = buildAIPrompt(monthTxs, monthLabel);
    Telegram.haptic('medium');
    openAIModal(prompt);
  });
}
