import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ensureClientRequestId,
  friendlySubmitError,
  normalizeQuickTemplates,
  templateToDraft,
} from './add-flow.js';

test('network failures are presented in clear Ukrainian', () => {
  assert.equal(
    friendlySubmitError(new TypeError('Failed to fetch'), 'Не вдалося зберегти'),
    'Немає з’єднання. Перевірте інтернет і спробуйте ще раз.',
  );
  assert.equal(friendlySubmitError(new Error('Сервер відмовив'), 'Fallback'), 'Сервер відмовив');
  assert.equal(friendlySubmitError(null, 'Fallback'), 'Fallback');
});

test('retry reuses one client request id until the caller clears it after success', () => {
  let calls = 0;
  const createId = () => `request-${++calls}`;

  const first = ensureClientRequestId(null, createId);
  const retry = ensureClientRequestId(first, createId);
  const afterSuccess = ensureClientRequestId(null, createId);

  assert.equal(first, 'request-1');
  assert.equal(retry, first);
  assert.equal(afterSuccess, 'request-2');
  assert.equal(calls, 2);
});

test('normalizes backend quick templates and preserves comment aliases', () => {
  const normalized = normalizeQuickTemplates({
    templates: [
      { type: 'expense', amount: 40, currency: 'UAH', category: 'Кафе', comment: 'Кава', usage_count: 9 },
      { type: 'invalid', amount: 10, currency: 'UAH', category: 'Ignore' },
    ],
    last_operation: {
      type: 'income', amount: 100, currency: 'USD', category: 'Фріланс', subcategory: 'Клієнт A', comment: 'Рахунок №7',
    },
  });

  assert.equal(normalized.templates.length, 1);
  assert.equal(normalized.templates[0].category, 'Кафе');
  assert.equal(normalized.templates[0].description, 'Кава');
  assert.equal(normalized.templates[0].useCount, 9);
  assert.equal(normalized.repeatLast.category, 'Фріланс');
  assert.equal(normalized.repeatLast.subcategory, 'Клієнт A');
  assert.equal(normalized.repeatLast.description, 'Рахунок №7');
});

test('also accepts legacy last_transaction and description response names', () => {
  const normalized = normalizeQuickTemplates({
    templates: [],
    last_transaction: {
      type: 'expense', amount: 15, currency: 'UAH', category: 'Інше', description: 'Legacy',
    },
  });

  assert.equal(normalized.repeatLast.description, 'Legacy');
});

test('template prefill remains editable and drops stale category hierarchy', () => {
  const valid = templateToDraft({
    type: 'expense', amount: 25.5, currency: 'EUR', category: 'Кафе',
    subcategory: 'Обіди', description: 'Кава з клієнтом',
  }, {
    categories: ['Кафе', 'Інше'],
    subcategories: ['Обіди'],
  });
  assert.deepEqual(valid, {
    mode: 'expense', amount: '25.5', currency: 'EUR', category: 'Кафе',
    subcategory: 'Обіди', note: 'Кава з клієнтом', paymentSource: null,
  });

  const stale = templateToDraft({
    type: 'expense', amount: 10, currency: 'UAH', category: 'Видалена', subcategory: 'Стара',
  }, {
    categories: ['Інше'],
    subcategories: [],
  });
  assert.equal(stale.category, null);
  assert.equal(stale.subcategory, null);
});

test('Add screen wires the backend contract, retry-safe write and persistent save dock', async () => {
  const [apiSource, addSource, cssSource] = await Promise.all([
    readFile(new URL('./api.js', import.meta.url), 'utf8'),
    readFile(new URL('./screens/add.js', import.meta.url), 'utf8'),
    readFile(new URL('../css/add-enhancements.css', import.meta.url), 'utf8'),
  ]);

  assert.match(apiSource, /quickTemplates:\s*\(\)\s*=>\s*request\('\/api\/quick-templates'\)/);
  assert.match(addSource, /client_request_id:\s*state\.clientRequestId/);
  assert.match(addSource, /if \(state\.submitting \|\| state\.submitSuccess\) return;/);
  assert.match(addSource, /disabled = state\.submitting/);
  assert.match(cssSource, /\.add-submit-dock\s*{[^}]*position:\s*fixed/s);
  assert.match(cssSource, /bottom:\s*calc\(var\(--nav-h\)/);
});
