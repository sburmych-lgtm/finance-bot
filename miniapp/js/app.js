/* Ruby Finance — app shell, router, state */

import { Telegram } from './telegram.js';
import { Api } from './api.js';
import { fmtMoney, fmtDate, toast } from './ui.js';
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
  rates: { USD: 41.5, EUR: 45.2 },
  screen: 'home',

  async hydrate() {
    const now = new Date();
    try {
      const [me, balance, txs, cats, rates] = await Promise.all([
        Api.me().catch(() => null),
        Api.getBalance(now.getFullYear(), now.getMonth() + 1).catch(() => null),
        Api.listTransactions(15).catch(() => []),
        Api.categories().catch(() => null),
        Api.exchangeRates().catch(() => null),
      ]);
      this.user = me;
      this.balance = balance;
      this.transactions = txs || [];
      this.categories = cats;
      if (rates) Object.assign(this.rates, rates);
    } catch (e) {
      console.warn('hydrate failed', e);
    }
  },
};

window.Ruby = { Store, Api, Telegram, toast, fmtMoney, fmtDate };

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
    navigate(goBtn.dataset.go, opts);
  }
});

async function boot() {
  await Store.hydrate();
  navigate('home');
}

boot();
