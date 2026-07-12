import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import {
  normalizeBudgetResponse,
  normalizePaymentSourceBreakdown,
  normalizePaymentSource,
  paymentSourceLabel,
  paymentSourceOptions,
} from './block2-ui.js';
import { normalizeQuickTemplates, templateToDraft } from './add-flow.js';

test('payment source labels never infer a legacy null value', () => {
  assert.equal(normalizePaymentSource(null), null);
  assert.equal(normalizePaymentSource('cash'), 'cash');
  assert.equal(normalizePaymentSource('bank'), null);
  assert.equal(paymentSourceLabel(null), 'Не вказано');
  assert.deepEqual(paymentSourceOptions().map(({ value }) => value), [
    'cash', 'card', 'transfer', 'other',
  ]);
});

test('source breakdown keeps legacy rows in an explicit unclassified bucket', () => {
  assert.deepEqual(normalizePaymentSourceBreakdown({
    expense_by_payment_source: { cash: 50, card: 20, unclassified: 10 },
  }), [
    { source: 'cash', label: 'Готівка', value: 50 },
    { source: 'card', label: 'Картка', value: 20 },
    { source: null, label: 'Не вказано', value: 10 },
  ]);
});

test('quick templates preserve an explicit or legacy-null payment source', () => {
  const normalized = normalizeQuickTemplates({
    templates: [
      { type: 'expense', amount: 20, currency: 'UAH', category: 'Кафе', payment_source: 'cash' },
      { type: 'expense', amount: 30, currency: 'UAH', category: 'Кафе', payment_source: null },
    ],
  });

  assert.equal(normalized.templates[0].paymentSource, 'cash');
  assert.equal(normalized.templates[1].paymentSource, null);
  assert.equal(templateToDraft(normalized.templates[0], { categories: ['Кафе'] }).paymentSource, 'cash');
  assert.equal(templateToDraft(normalized.templates[1], { categories: ['Кафе'] }).paymentSource, null);
});

test('budget response is normalized without losing exact progress state', () => {
  const budgets = normalizeBudgetResponse({ budgets: [{
    type: 'expense',
    category: 'Кафе',
    monthly_limit_uah: '1000.50',
    spent_uah: 840.25,
    remaining_uah: 160.25,
    progress_percent: 84.01,
    is_exceeded: false,
  }] });

  assert.deepEqual(budgets, [{
    type: 'expense',
    category: 'Кафе',
    monthlyLimit: 1000.5,
    spent: 840.25,
    remaining: 160.25,
    progressPercent: 84.01,
    isExceeded: false,
  }]);
  assert.deepEqual(normalizeBudgetResponse({ budgets: 'bad' }), []);
});

test('API exposes owner-scoped transaction source and budget methods', () => {
  const source = fs.readFileSync(new URL('./api.js', import.meta.url), 'utf8');
  assert.match(source, /patchTransaction:\s*\(id, payload\).*\/api\/transactions\/\$\{id\}/);
  assert.match(source, /budgets:\s*\(year, month\).*\/api\/budgets\?year=\$\{year\}&month=\$\{month\}/);
  assert.match(source, /upsertBudget:\s*\(payload\).*method: 'PUT'/);
  assert.match(source, /deleteBudget:\s*\(type, category\)/);
});

test('generic Add stays unclassified while Home cash entry is explicit', () => {
  const addSource = fs.readFileSync(new URL('./screens/add.js', import.meta.url), 'utf8');
  const appSource = fs.readFileSync(new URL('./app.js', import.meta.url), 'utf8');
  const indexSource = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');

  assert.match(addSource, /paymentSource:\s*null/);
  assert.match(addSource, /state\.paymentSource = null/);
  assert.match(addSource, /payment_source:\s*state\.paymentSource/);
  assert.match(addSource, /state\.paymentSource = opts\.paymentSource/);
  assert.match(appSource, /opts\.paymentSource = goBtn\.dataset\.paymentSource/);
  assert.match(indexSource, /data-kind="expense" data-payment-source="cash"/);
});

test('History refetches when returning to a previously selected calendar month', () => {
  const source = fs.readFileSync(new URL('./screens/history.js', import.meta.url), 'utf8');
  assert.match(source, /state\.period = b\.dataset\.period;\s*Telegram\.haptic\('selection'\);\s*fetchRows\(\);/s);
  assert.doesNotMatch(source, /state\.period !== 'month'/);
});

test('budget editor creates expense limits only', () => {
  const source = fs.readFileSync(new URL('./screens/settings.js', import.meta.url), 'utf8');
  assert.match(source, /const budgets = normalizeBudgetResponse\(response\)\.filter\(\(budget\) => budget\.type === 'expense'\)/);
  assert.match(source, /type:\s*'expense'/);
  assert.doesNotMatch(source, /id="budgetType"/);
});
