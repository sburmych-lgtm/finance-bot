/* Ruby Finance — app shell, router, state */

import { Telegram } from './telegram.js';
import { Api } from './api.js';
import { fmtMoney, fmtDate, toast, el } from './ui.js';
import { renderHome } from './screens/home.js';
import { renderAdd } from './screens/add.js';
import { renderReports } from './screens/reports.js';
import { renderHistory } from './screens/history.js';
import { renderSettings } from './screens/settings.js';

const screens = {
  home:     { title: 'Огляд',         render: renderHome },
  add:      { title: 'Додати',        render: renderAdd },
  reports:  { title: 'Звіти',         render: renderReports },
  history:  { title: 'Історія',       render: renderHistory },
  settings: { title: 'Налаштування',  render: renderSettings },
};

export const Store = {
  user: null,
  balance: null,
  transactions: [],
  categories: null,
  categoriesFull: null,   // full dict {expense:{name:{emoji,keywords,subcategories}}, income:{...}}
  rates: { USD: 41.5, EUR: 45.2 },
  timeCategories: null,
  employees: [],
  budgets: [],
  screen: 'home',

  async hydrate() {
    const now = new Date();
    try {
      const [me, balance, txs, cats, catsFull, rates, tCats, emps, budgetResponse] = await Promise.all([
        Api.me().catch(() => null),
        Api.getBalance(now.getFullYear(), now.getMonth() + 1).catch(() => null),
        Api.listTransactions(15).catch(() => []),
        Api.categories().catch(() => null),
        Api.categoriesFull().catch(() => null),
        Api.exchangeRates().catch(() => null),
        Api.timeCategories().catch(() => null),
        Api.employees().catch(() => []),
        Api.budgets(now.getFullYear(), now.getMonth() + 1).catch(() => ({ budgets: [] })),
      ]);
      this.user = me;
      this.balance = balance;
      this.transactions = txs || [];
      this.categories = cats;
      this.categoriesFull = catsFull;
      if (rates) Object.assign(this.rates, rates);
      this.timeCategories = tCats;
      this.employees = emps || [];
      this.budgets = Array.isArray(budgetResponse?.budgets) ? budgetResponse.budgets : [];
    } catch (e) {
      console.warn('hydrate failed', e);
    }
  },

  // Subcategory names for a given category, or [] if none.
  subcategoriesFor(mode, category) {
    const def = this.categoriesFull?.[mode]?.[category];
    const subs = def && Array.isArray(def.subcategories) ? def.subcategories : [];
    return subs;
  },
};

window.Ruby = { Store, Api, Telegram, toast, fmtMoney, fmtDate };

// ── Paywall modal (Крок 5) — shown when a write returns 402 PAYWALL ──
function showPaywallModal(pw = {}) {
  const price = pw.price || 199;
  const jar = pw.jar_url || '';
  const trialDays = pw.trial_days || 7;
  const eligible = !!pw.trial_eligible;
  document.getElementById('paywallModal')?.remove();

  const card = el('div', { class: 'paywall-card' });
  card.appendChild(el('div', { class: 'paywall-emoji' }, eligible ? '🎁' : '🔒'));
  card.appendChild(el('div', { class: 'paywall-title' },
    eligible ? 'Як продовжити?' : 'Потрібна підписка'));
  card.appendChild(el('div', { class: 'paywall-text' }, eligible
    ? `Спробуйте безкоштовно ${trialDays} днів або оформіть підписку ${price} ₴/міс. Переглядати дані та звіти можна безкоштовно.`
    : `Щоб додавати операції — ${price} ₴/міс. Переглядати дані та звіти можна безкоштовно.`));

  if (eligible) {
    const trialBtn = el('button', { class: 'btn btn-primary', type: 'button' },
      `🎁 Спробувати безкоштовно ${trialDays} днів`);
    trialBtn.addEventListener('click', async () => {
      trialBtn.disabled = true;
      try {
        const r = await Api.trialStart();
        Telegram.haptic('success');
        toast(r?.message || `Активовано ${trialDays} днів безкоштовно!`, 4000);
        document.getElementById('paywallModal')?.remove();
        await Store.hydrate();
      } catch (_) {
        toast('Не вдалося активувати. Спробуйте ще раз.');
        trialBtn.disabled = false;
      }
    });
    card.appendChild(trialBtn);
  }

  if (jar) {
    card.appendChild(el('a', {
      class: `btn ${eligible ? 'btn-secondary' : 'btn-primary'} paywall-pay`,
      href: jar, target: '_blank', rel: 'noopener',
    }, `💳 Оформити підписку ${price} ₴`));
  }

  const paid = el('button', { class: 'btn btn-ghost', type: 'button' }, '✅ Я оплатив');
  paid.addEventListener('click', async () => {
    paid.disabled = true;
    try {
      const r = await Api.paymentClaim();
      toast(r?.message || 'Заявку надіслано, очікуйте підтвердження.', 4500);
      document.getElementById('paywallModal')?.remove();
    } catch (_) {
      toast('Не вдалося надіслати заявку. Спробуйте ще раз.');
      paid.disabled = false;
    }
  });
  card.appendChild(paid);

  const later = el('button', { class: 'btn btn-ghost', type: 'button' }, 'Пізніше');
  later.addEventListener('click', () => document.getElementById('paywallModal')?.remove());
  card.appendChild(later);

  const overlay = el('div', { id: 'paywallModal', class: 'paywall-overlay' }, card);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  Telegram.haptic('warning');
}

window.addEventListener('ruby:paywall', (e) => showPaywallModal(e.detail || {}));

let authExpiryShown = false;
window.addEventListener('ruby:auth-expired', () => {
  if (authExpiryShown) return;
  authExpiryShown = true;
  const message = 'Сесія Telegram завершилась. Закрийте Mini App і відкрийте його з бота ще раз.';
  Telegram.haptic('warning');
  const shown = Telegram.showPopup({
    title: 'Потрібно оновити сесію',
    message,
    buttons: [{ id: 'close', type: 'close' }],
  }, () => Telegram.close());
  if (!shown) toast(message, 8000);
});

export function navigate(screen, opts = {}) {
  if (!screens[screen]) return;
  Store.screen = screen;
  Store.nav_opts = opts;
  document.querySelectorAll('.screen').forEach((s) => {
    s.classList.toggle('active', s.dataset.screen === screen);
  });
  document.querySelectorAll('.nav-item').forEach((n) => {
    n.classList.toggle('active', n.dataset.nav === screen);
  });
  const titleEl = document.getElementById('screenTitle');
  if (titleEl) titleEl.textContent = screens[screen].title;
  Telegram.haptic('selection');
  screens[screen].render?.(opts);
}

document.addEventListener('click', (e) => {
  const navBtn = e.target.closest('[data-nav]');
  if (navBtn) {
    e.preventDefault();
    navigate(navBtn.dataset.nav);
    return;
  }
  const goBtn = e.target.closest('[data-go]');
  if (goBtn) {
    e.preventDefault();
    const opts = {};
    if (goBtn.dataset.kind) opts.kind = goBtn.dataset.kind;
    if (goBtn.dataset.paymentSource) opts.paymentSource = goBtn.dataset.paymentSource;
    if (goBtn.dataset.section) opts.section = goBtn.dataset.section;
    navigate(goBtn.dataset.go, opts);
  }
});

async function boot() {
  await Store.hydrate();
  navigate('home');
}

boot();
