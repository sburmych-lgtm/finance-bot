/* Reports screen */

import { Store } from '../app.js';
import { fmtMoney, esc } from '../ui.js';

const SLICE_COLORS = ['#6E0F1F', '#D8B56D', '#9B1B30', '#6FB67E', '#D45A4F', '#7A6E66'];

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
  `;
}
