/* Import screen — CSV bank-statement import (Block 4, stage 5)
 *
 * Flow: choose/paste a CSV → POST /api/import/preview (parses, flags likely
 * duplicates, writes nothing) → user reviews rows (toggle include, pick a
 * category) → POST /api/import/confirm (one atomic, rollback-able batch).
 * A "recent imports" list lets the user roll a whole batch back.
 *
 * Markup is assembled from esc()-escaped values (trusted-by-construction) and
 * written via the shared setHTML helper, matching the rest of the Mini App.
 */

import { Store } from '../app.js';
import { Api } from '../api.js';
import { Telegram } from '../telegram.js';
import { toast, esc, fmtDate, setHTML } from '../ui.js';

const MAX_FILE_BYTES = 2 * 1024 * 1024; // 2 MB — matches the server's char cap

const state = {
  step: 'input',   // 'input' | 'preview'
  rows: [],        // [{date,type,amount,currency,description,duplicate,category,include}]
  errors: [],
  summary: null,
  source: 'csv',
  busy: false,
};

function reset() {
  state.step = 'input';
  state.rows = [];
  state.errors = [];
  state.summary = null;
  state.source = 'csv';
  state.busy = false;
}

function categoryOptions(type, selected) {
  const cats = (Store.categories && Store.categories[type]) || ['Інше'];
  const chosen = selected || 'Інше';
  const list = cats.includes(chosen) ? cats : [chosen, ...cats];
  return list
    .map((c) => `<option value="${esc(c)}"${c === chosen ? ' selected' : ''}>${esc(c)}</option>`)
    .join('');
}

function amountLabel(row) {
  const abs = Math.abs(Number(row.amount) || 0).toLocaleString('uk-UA', { maximumFractionDigits: 2 });
  const sym = row.currency === 'USD' ? '$' : row.currency === 'EUR' ? '€' : '₴';
  return `${row.type === 'expense' ? '−' : '+'}${abs} ${sym}`;
}

function screenRoot(node) {
  return node.closest('#screen-settings') || node.parentNode;
}

// ── Preview / confirm / rollback actions ───────────────────────
async function doPreview(root, csv, source) {
  if (!csv || !csv.trim()) { toast('Порожній файл або текст'); return; }
  state.busy = true;
  renderBody(root);
  try {
    const res = await Api.importPreview(csv);
    state.rows = (res.rows || []).map((r) => ({
      ...r,
      category: 'Інше',
      include: !r.duplicate, // duplicates start unchecked
    }));
    state.errors = res.errors || [];
    state.summary = res.summary || null;
    state.source = source || 'csv';
    state.step = 'preview';
    Telegram.haptic('light');
  } catch (e) {
    toast(e.message || 'Не вдалося розібрати файл');
  } finally {
    state.busy = false;
    renderBody(root);
  }
}

async function doConfirm(root) {
  const chosen = state.rows.filter((r) => r.include).map((r) => ({
    date: r.date,
    type: r.type,
    amount: r.amount,
    currency: r.currency,
    description: r.description,
    category: r.category || 'Інше',
  }));
  if (!chosen.length) { toast('Оберіть хоча б одну операцію'); return; }
  state.busy = true;
  renderBody(root);
  try {
    const res = await Api.importConfirm(chosen, state.source);
    Telegram.haptic('success');
    const skipped = res.skipped ? `, пропущено ${res.skipped}` : '';
    toast(`Імпортовано ${res.imported} операцій${skipped}`);
    await Store.hydrate(); // refresh balance/transactions so Home reflects the import
    reset();
    renderBody(root);
    loadBatches(root);
  } catch (e) {
    state.busy = false;
    toast(e.message || 'Помилка імпорту');
    renderBody(root);
  }
}

function confirmRollback(batch, onYes) {
  const shown = Telegram.showPopup(
    {
      title: 'Відкотити імпорт?',
      message: `Буде видалено ${batch.row_count || 0} операцій. Дію не можна скасувати.`,
      buttons: [
        { id: 'yes', type: 'destructive', text: 'Відкотити' },
        { id: 'no', type: 'cancel' },
      ],
    },
    (id) => { if (id === 'yes') onYes(); },
  );
  if (!shown) {
    // Browser / older client fallback
    if (window.confirm(`Відкотити ${batch.row_count || 0} операцій цього імпорту?`)) onYes();
  }
}

async function doRollback(root, id, batch) {
  confirmRollback(batch, async () => {
    try {
      const res = await Api.importBatchDelete(id);
      Telegram.haptic('success');
      toast(`Відкочено ${res.deleted_transactions ?? 0} операцій`);
      await Store.hydrate();
      loadBatches(root);
    } catch (e) {
      toast(e.message || 'Не вдалося відкотити');
    }
  });
}

// ── Recent imports (batches) ───────────────────────────────────
async function loadBatches(root) {
  const list = root.querySelector('#importBatchesList');
  const section = root.querySelector('#importBatchesSection');
  if (!list || !section) return;
  try {
    const res = await Api.importBatches();
    const batches = res.batches || [];
    if (!batches.length) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';
    setHTML(list, batches.map((b) => `
      <div class="row">
        <div class="avatar">↓</div>
        <div>
          <div class="row-title">${esc(b.source || 'csv')}</div>
          <div class="row-meta">${esc(fmtDate(b.created_at))} · ${b.row_count || 0} операцій</div>
        </div>
        <button type="button" class="btn btn-ghost import-rollback" data-id="${esc(b.id)}">Відкотити</button>
      </div>
    `).join(''));
    list.querySelectorAll('.import-rollback').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = Number(btn.dataset.id);
        const batch = batches.find((x) => x.id === id) || {};
        doRollback(root, id, batch);
      });
    });
  } catch (e) {
    section.style.display = 'none';
  }
}

// ── Rendering ──────────────────────────────────────────────────
function renderInput(body) {
  setHTML(body, `
    <div class="panel import-intro">
      <div class="import-intro-title">Імпорт виписки CSV</div>
      <div class="import-intro-text">
        Завантажте CSV-виписку з банку (Приват24, монобанк, будь-який експорт).
        Ми розпізнаємо дату, суму й опис, покажемо для перевірки та позначимо можливі
        дублікати. Нічого не запишеться, поки ви не підтвердите.
      </div>
    </div>

    <div class="setting-section">
      <label class="btn btn-primary import-file-btn" for="importFile">📄 Обрати файл CSV</label>
      <input type="file" id="importFile" accept=".csv,text/csv,text/plain" hidden>
    </div>

    <div class="setting-section">
      <div class="section-head"><div class="section-title">…або вставте текст</div></div>
      <div class="field">
        <textarea class="input import-textarea" id="importPaste" rows="6"
          placeholder="Дата,Сума,Опис&#10;13.07.2026,-250.50,Кава&#10;12.07.2026,+30000,Зарплата"></textarea>
      </div>
      <button type="button" class="btn btn-secondary" id="importParseBtn"${state.busy ? ' disabled' : ''}>
        ${state.busy ? 'Аналізуємо…' : 'Перевірити'}
      </button>
    </div>
  `);

  const fileInput = body.querySelector('#importFile');
  fileInput?.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    if (file.size > MAX_FILE_BYTES) { toast('Файл завеликий (макс 2 МБ)'); fileInput.value = ''; return; }
    const reader = new FileReader();
    reader.onload = () => doPreview(screenRoot(body), String(reader.result || ''), file.name);
    reader.onerror = () => toast('Не вдалося прочитати файл');
    reader.readAsText(file, 'utf-8');
  });
  body.querySelector('#importParseBtn')?.addEventListener('click', () => {
    const txt = body.querySelector('#importPaste')?.value || '';
    doPreview(screenRoot(body), txt, 'вставлений текст');
  });
}

function renderPreview(body) {
  const s = state.summary || {};
  const errorsNote = state.errors.length
    ? `<div class="import-errors">⚠️ ${state.errors.length} рядків не розпізнано й пропущено</div>`
    : '';
  const rowsHtml = state.rows.map((r, i) => `
    <label class="import-row${r.duplicate ? ' is-dup' : ''}">
      <input type="checkbox" class="import-include" data-i="${i}"${r.include ? ' checked' : ''}>
      <div class="import-row-main">
        <div class="import-row-top">
          <span class="import-date">${esc(fmtDate(r.date))}</span>
          <span class="import-amount ${esc(r.type)}">${esc(amountLabel(r))}</span>
        </div>
        <div class="import-desc">${esc(r.description || '—')}${r.duplicate ? ' <span class="badge-dup">можливий дубль</span>' : ''}</div>
        <select class="input import-cat" data-i="${i}">${categoryOptions(r.type, r.category)}</select>
      </div>
    </label>
  `).join('');

  const includedCount = state.rows.filter((r) => r.include).length;
  setHTML(body, `
    <div class="panel import-summary">
      <div>Знайдено <strong>${esc(s.total ?? state.rows.length)}</strong> операцій:
        ${esc(s.income ?? 0)} дох. / ${esc(s.expense ?? 0)} витр.${s.duplicates ? ` · <span class="import-dup-count">${esc(s.duplicates)} дублів</span>` : ''}</div>
      ${errorsNote}
    </div>
    <div class="import-rows" id="importRows">${rowsHtml || '<div class="empty-state">Немає операцій для імпорту</div>'}</div>
    <div class="import-actions">
      <button type="button" class="btn btn-ghost" id="importCancelBtn"${state.busy ? ' disabled' : ''}>Скасувати</button>
      <button type="button" class="btn btn-primary" id="importConfirmBtn"${state.busy ? ' disabled' : ''}>
        ${state.busy ? 'Імпортуємо…' : `Імпортувати (${includedCount})`}
      </button>
    </div>
  `);

  body.querySelectorAll('.import-include').forEach((cb) => {
    cb.addEventListener('change', () => {
      const i = Number(cb.dataset.i);
      if (state.rows[i]) state.rows[i].include = cb.checked;
      const btn = body.querySelector('#importConfirmBtn');
      const n = state.rows.filter((r) => r.include).length;
      if (btn && !state.busy) btn.textContent = `Імпортувати (${n})`;
    });
  });
  body.querySelectorAll('.import-cat').forEach((sel) => {
    sel.addEventListener('change', () => {
      const i = Number(sel.dataset.i);
      if (state.rows[i]) state.rows[i].category = sel.value;
    });
  });
  const rootEl = screenRoot(body);
  body.querySelector('#importCancelBtn')?.addEventListener('click', () => { reset(); renderBody(rootEl); });
  body.querySelector('#importConfirmBtn')?.addEventListener('click', () => doConfirm(rootEl));
}

function renderBody(root) {
  const body = root.querySelector('#importBody');
  if (!body) return;
  if (state.step === 'preview') renderPreview(body);
  else renderInput(body);
}

export function renderImport(root, onBack) {
  reset();
  setHTML(root, `
    <div class="settings-back" id="importBack">
      <button class="ghost-btn" aria-label="Назад">‹</button>
      <div class="settings-back-title">Імпорт виписки</div>
    </div>
    <div id="importBody"></div>
    <div class="setting-section" id="importBatchesSection" style="display:none;">
      <div class="section-head"><div class="section-title">Останні імпорти</div></div>
      <div class="row-list" id="importBatchesList"></div>
    </div>
  `);
  root.querySelector('#importBack')?.addEventListener('click', () => { reset(); (onBack || (() => {}))(); });
  renderBody(root);
  loadBatches(root);
}
