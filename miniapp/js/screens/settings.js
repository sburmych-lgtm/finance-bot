/* Settings screen */

import { Telegram } from '../telegram.js';
import { Store } from '../app.js';
import { toast, esc } from '../ui.js';

export function renderSettings() {
  const root = document.getElementById('screen-settings');
  if (!root) return;

  const rates = Store.rates || { USD: 41.5, EUR: 45.2 };
  const user = Telegram.user;
  const firstName = String(user?.first_name || 'Користувач');
  const lastName  = String(user?.last_name || '');
  const initial   = (firstName[0] || 'R').toUpperCase();

  root.innerHTML = `
    <div class="panel" style="padding: var(--sp-4);">
      <div class="brand" style="gap: var(--sp-4);">
        <div class="monogram">${esc(initial)}</div>
        <div class="wordmark">
          <div class="eyebrow">Профіль</div>
          <div class="screen-title" style="font-size: var(--fs-17); margin-top: 0;">
            ${esc(firstName)} ${esc(lastName)}
          </div>
        </div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Категорії</div></div>
      <div class="row-list">
        <div class="row" data-stub><div class="avatar">E</div>
          <div><div class="row-title">Витрати</div><div class="row-meta">15 базових категорій</div></div>
          <div class="row-chevron">›</div></div>
        <div class="row" data-stub><div class="avatar">I</div>
          <div><div class="row-title">Доходи</div><div class="row-meta">7 категорій</div></div>
          <div class="row-chevron">›</div></div>
        <div class="row" data-stub><div class="avatar">T</div>
          <div><div class="row-title">Час</div><div class="row-meta">Активності для трекінгу</div></div>
          <div class="row-chevron">›</div></div>
      </div>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">Курси валют</div></div>
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
        <div class="row" data-stub><div class="avatar">i</div>
          <div><div class="row-title">Як зберігаються дані</div><div class="row-meta">Railway Volume, ізоляція за Telegram ID</div></div>
          <div class="row-chevron">›</div></div>
        <div class="row" data-stub><div class="avatar">×</div>
          <div><div class="row-title">Очистити мої дані</div><div class="row-meta">видалити всі операції назавжди</div></div>
          <div class="row-chevron">›</div></div>
      </div>
    </div>

    <div class="setting-section">
      <button class="btn btn-ghost" id="closeApp">Закрити Mini App</button>
    </div>
  `;

  root.querySelectorAll('[data-stub]').forEach((r) => r.addEventListener('click', () => {
    Telegram.haptic('warning');
    toast('Скоро в наступному оновленні');
  }));
  root.querySelector('#closeApp')?.addEventListener('click', () => Telegram.close());
}
