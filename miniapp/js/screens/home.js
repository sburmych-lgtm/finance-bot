/* Home / Огляд */

import { Store } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { fmtMoney, fmtAmount, fmtDate, esc, toast, setHTML } from '../ui.js';
import { normalizeBudgetResponse, paymentSourceLabel, budgetTone } from '../block2-ui.js';
import { insightPresentation, normalizeInsights } from '../automation-ui.js';
import { findDirectSectionHead } from '../home-layout.js';

const CATEGORY_LETTER = {
  'Продукти':'П','Кафе':'К','Транспорт':'Т','Розваги':'Р','Здоров\'я':'Z',
  'Ліки':'L','Подарунки':'G','Податки':'%','Косметолог':'C','Салон краси':'S',
  'Догляд/Косметика':'D','Вітаміни':'V','Одяг':'O','Комунальні':'H','Інше':'•',
  'Зарплата':'$','Фріланс':'F','Консультації':'L','ВЛК':'M','ТЦК':'M','Суди':'J',
};

function letter(cat) { return CATEGORY_LETTER[cat] || (cat?.[0] || '•').toUpperCase(); }
let insightGeneration = 0;

export function renderHome() {
  const card = document.querySelector('#screen-home .balance-card');
  if (!card) return;

  const b = Store.balance;
  if (b) {
    const month = new Date().toLocaleDateString('uk-UA', { month: 'long', year: 'numeric' });
    card.querySelector('.balance-value').textContent = fmtMoney(b.balance || 0, 'UAH');
    card.querySelector('.balance-value').classList.remove('sk');
    card.querySelector('.balance-sub').textContent = `${month} · чистий результат`;
    card.querySelector('.balance-sub').classList.remove('sk');
    const metrics = card.querySelectorAll('.metric strong');
    metrics[0].textContent = fmtAmount(b.income || 0, 'UAH');
    metrics[1].textContent = fmtAmount(b.expense || 0, 'UAH');
    metrics.forEach((m) => m.classList.remove('sk'));
  }

  // Inject «Відмінити останню» quick-action card if there's something to undo
  const txs = (Store.transactions || []).slice(0, 8);
  injectUndoCard(txs[0]);
  injectBudgetOverview();
  renderHomeInsights();

  const list = document.getElementById('recent-list');
  if (!list) return;
  if (!txs.length) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="icon">∅</div>
        <h3>Поки що порожньо</h3>
        <p>Додайте першу операцію через кнопку «+ Додати» — або введіть текстом у чаті бота.</p>
      </div>`;
    return;
  }

  list.innerHTML = txs.map((t) => `
    <div class="row">
      <div class="avatar">${esc(letter(t.category))}</div>
      <div>
        <div class="row-title">${esc(t.category || 'Інше')}</div>
        <div class="row-meta">${esc(fmtDate(t.date))} · ${esc(paymentSourceLabel(t.payment_source))} · ${esc(String(t.description || '').slice(0, 32))}</div>
      </div>
      <div class="amount ${t.type === 'expense' ? 'expense' : 'income'}">${esc(fmtMoney(
        t.type === 'expense' ? -(t.amount_uah || t.amount) : (t.amount_uah || t.amount),
        'UAH'
      ))}</div>
    </div>
  `).join('');
}

async function renderHomeInsights() {
  const generation = ++insightGeneration;
  document.getElementById('home-insights')?.remove();
  const anchor = document.querySelector('#screen-home .quick-actions');
  if (!anchor) return;

  const section = document.createElement('section');
  section.id = 'home-insights';
  section.className = 'home-insights';
  setHTML(section, `
    <div class="section-head"><div class="section-title">Розумні підказки</div></div>
    <div class="panel insight-loading" aria-busy="true"><span class="sk"></span><span class="sk"></span></div>
  `);
  anchor.after(section);

  try {
    const insights = normalizeInsights(await Api.insights());
    if (generation !== insightGeneration || Store.screen !== 'home' || !section.isConnected) return;
    const cards = insights.slice(0, 4).map((insight) => insightPresentation(insight)).filter(Boolean);
    setHTML(section, `
      <div class="section-head"><div class="section-title">Розумні підказки</div><span class="section-link">без AI</span></div>
      ${cards.length ? `
        <div class="insight-strip" aria-label="Фінансові підказки">
          ${cards.map((card) => `
            <article class="panel insight-card ${esc(card.tone)}">
              <div class="insight-icon" aria-hidden="true">${esc(card.icon)}</div>
              <div><h3>${esc(card.title)}</h3><p>${esc(card.body)}</p></div>
            </article>
          `).join('')}
        </div>
      ` : `
        <div class="panel insight-empty">
          <span class="insight-icon" aria-hidden="true">◇</span>
          <span><strong>Фінансовий ритм формується</strong><small>Підказки з’являться, коли буде достатньо операцій для чесного порівняння.</small></span>
        </div>
      `}
    `);
  } catch (error) {
    if (generation !== insightGeneration || Store.screen !== 'home' || !section.isConnected) return;
    setHTML(section, `
      <div class="section-head"><div class="section-title">Розумні підказки</div></div>
      <div class="panel insight-error" role="status">
        <span>Не вдалося завантажити підказки.</span>
        <button type="button" class="btn btn-secondary insight-retry">Повторити</button>
      </div>
    `);
    section.querySelector('.insight-retry')?.addEventListener('click', renderHomeInsights);
  }
}

function injectBudgetOverview() {
  document.getElementById('home-budget-overview')?.remove();
  const screen = document.getElementById('screen-home');
  const sectionHead = findDirectSectionHead(screen);
  if (!sectionHead) return;
  const budgets = normalizeBudgetResponse({ budgets: Store.budgets })
    .filter((budget) => budget.type === 'expense');
  const wrapper = document.createElement('section');
  wrapper.id = 'home-budget-overview';
  wrapper.className = 'home-budget-overview';
  setHTML(wrapper, `
    <div class="section-head">
      <div class="section-title">Бюджети</div>
      <button class="section-link" data-go="settings" data-section="budgets">Керувати →</button>
    </div>
    ${budgets.length ? `
      <div class="panel budget-compact-list">
        ${budgets.slice(0, 3).map((budget) => `
          <div class="budget-compact ${budgetTone(budget)}">
            <div class="budget-progress-head">
              <strong>${esc(budget.category)}</strong>
              <span>${esc(fmtAmount(budget.spent, 'UAH'))} / ${esc(fmtAmount(budget.monthlyLimit, 'UAH'))}</span>
            </div>
            <div class="budget-track" role="progressbar" aria-label="${esc(`Бюджет ${budget.category}`)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.min(100, Math.round(budget.progressPercent))}">
              <span style="width:${Math.min(100, budget.progressPercent).toFixed(1)}%"></span>
            </div>
          </div>
        `).join('')}
      </div>
    ` : `
      <button type="button" class="panel budget-empty-cta" data-go="settings" data-section="budgets">
        <span class="avatar">◎</span>
        <span><strong>Встановити перший ліміт</strong><small>Контролюйте витрати по категоріях</small></span>
        <span aria-hidden="true">›</span>
      </button>
    `}
  `);
  screen.insertBefore(wrapper, sectionHead);
}


function injectUndoCard(lastTx) {
  // Place a discreet «Відмінити останню» button right above the recent-list
  // section so users always know the safety net is there. Mirrors the bot's
  // ↩️ Відмінити останню inline button.
  const existing = document.getElementById('undo-card');
  if (existing) existing.remove();

  if (!lastTx) return;
  const screen = document.getElementById('screen-home');
  const sectionHead = findDirectSectionHead(screen);
  if (!sectionHead) return;

  const card = document.createElement('div');
  card.id = 'undo-card';
  card.className = 'panel undo-card';
  const isExp = lastTx.type === 'expense';
  const amountStr = fmtMoney(
    isExp ? -(lastTx.amount_uah || lastTx.amount) : (lastTx.amount_uah || lastTx.amount),
    'UAH'
  );
  card.innerHTML = `
    <div class="undo-row">
      <div class="undo-icon">↩</div>
      <div class="undo-body">
        <div class="undo-title">Відмінити останню</div>
        <div class="undo-meta">${esc(lastTx.category || 'Інше')} · ${esc(amountStr)} · ${esc(fmtDate(lastTx.date))}</div>
      </div>
      <button class="undo-btn" id="undoBtn">Видалити</button>
    </div>`;
  screen.insertBefore(card, sectionHead);

  document.getElementById('undoBtn').addEventListener('click', async () => {
    const ok = window.confirm(`Видалити операцію «${lastTx.category} · ${amountStr}»? Цю дію неможливо скасувати.`);
    if (!ok) return;
    try {
      await Api.deleteTransaction(lastTx.id);
      Telegram.haptic('success');
      toast('Операцію відмінено');
      await Store.hydrate();
      renderHome();
    } catch (e) {
      Telegram.haptic('error');
      toast(e.message || 'Помилка');
    }
  });
}
