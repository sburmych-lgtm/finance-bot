import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

import {
  buildRecurringPatch,
  insightPresentation,
  normalizeDigest,
  normalizeForecast,
  normalizeInsights,
  normalizeNotificationSettings,
  normalizeRecurringOperations,
  normalizeRecurringSuggestions,
  recurrenceLabel,
} from './automation-ui.js';

test('recurring operations normalize complete backend rows without inventing values', () => {
  assert.deepEqual(normalizeRecurringOperations([{
    id: 7,
    type: 'expense',
    amount: '1200.50',
    currency: 'UAH',
    amount_uah: '1200.50',
    category: 'Оренда',
    subcategory: null,
    description: 'Офіс',
    payment_source: 'transfer',
    frequency: 'monthly',
    interval: 1,
    start_date: '2026-01-31',
    next_due_date: '2026-07-31',
    auto_create: true,
    active: false,
  }]), [{
    id: 7,
    type: 'expense',
    amount: 1200.5,
    currency: 'UAH',
    amountUah: 1200.5,
    category: 'Оренда',
    subcategory: null,
    description: 'Офіс',
    paymentSource: 'transfer',
    frequency: 'monthly',
    interval: 1,
    startDate: '2026-01-31',
    nextDueDate: '2026-07-31',
    autoCreate: true,
    active: false,
  }]);
  assert.deepEqual(normalizeRecurringOperations([{ id: 1, frequency: 'sometimes' }]), []);
  assert.equal(recurrenceLabel('monthly', 2), 'Кожні 2 місяці');
});

test('recurring edit patch does not reset an unchanged schedule', () => {
  const original = normalizeRecurringOperations([{
    id: 7, type: 'expense', amount: 1200, currency: 'UAH', amount_uah: 1200,
    category: 'Оренда', subcategory: null, description: 'Офіс', payment_source: 'transfer',
    frequency: 'monthly', interval: 1, start_date: '2026-01-31', next_due_date: '2026-08-31',
    auto_create: true, active: true,
  }])[0];
  const unchanged = {
    type: 'expense', amount: 1200, currency: 'UAH', category: 'Оренда',
    subcategory: null, description: 'Офіс', payment_source: 'transfer',
    frequency: 'monthly', interval: 1, start_date: '2026-01-31', auto_create: true,
  };
  assert.deepEqual(buildRecurringPatch(original, unchanged), {});
  assert.deepEqual(buildRecurringPatch(original, { ...unchanged, description: 'Новий офіс' }), {
    description: 'Новий офіс',
  });
});

test('suggestions retain conservative detector evidence and nullable source', () => {
  assert.deepEqual(normalizeRecurringSuggestions([{
    type: 'expense', category: 'Оренда', subcategory: 'Офіс',
    amount: '25', currency: 'USD', amount_uah: '900',
    payment_source: null, description: 'Офіс', frequency: 'monthly',
    occurrences: 3, last_date: '2026-03-31', next_date: '2026-04-30',
  }])[0], {
    type: 'expense', category: 'Оренда', subcategory: 'Офіс',
    amount: 25, currency: 'USD', amountUah: 900,
    paymentSource: null, description: 'Офіс', frequency: 'monthly',
    occurrences: 3, lastDate: '2026-03-31', nextDate: '2026-04-30',
  });
});

test('rule-based insights map to Ukrainian presentation and never require AI', () => {
  const insights = normalizeInsights([
    { kind: 'budget_warning', category: 'Кава', spent_uah: '900', limit_uah: '1000', percent: 90 },
    { kind: 'weekly_category_change', category: 'Кава', current_uah: '900', previous_uah: '600', percent: 50 },
    { kind: 'income_concentration', category: 'Клієнт A', amount_uah: '8000', percent: 80 },
  ]);
  assert.equal(insights.length, 3);
  assert.match(insightPresentation(insights[0]).title, /90%/);
  assert.match(insightPresentation(insights[1]).title, /\+50%/);
  assert.match(insightPresentation(insights[2]).body, /Клієнт A/);
  assert.equal(normalizeInsights([{ kind: 'made_up', category: 'x' }]).length, 0);
});

test('digest and notification settings are explicit opt-in', () => {
  assert.deepEqual(normalizeNotificationSettings({}), { weeklyDigestEnabled: false });
  assert.deepEqual(normalizeNotificationSettings({ weekly_digest_enabled: true }), { weeklyDigestEnabled: true });
  assert.deepEqual(normalizeDigest({
    period_start: '2026-07-06', period_end: '2026-07-12',
    total_income: '1000', total_expense: '650', net: '350',
    transaction_count: 4, top_expense_category: 'Їжа', top_expense_amount: '500',
  }), {
    periodStart: '2026-07-06', periodEnd: '2026-07-12',
    totalIncome: 1000, totalExpense: 650, net: 350,
    transactionCount: 4, topExpenseCategory: 'Їжа', topExpenseAmount: 500,
  });
});

test('forecast is accepted only as recorded-plus-scheduled month result', () => {
  assert.deepEqual(normalizeForecast({
    current_net: '7500', scheduled_income: '3000', scheduled_expense: '1500',
    estimated_tax: '1000', projected_result_before_tax: '9000',
    projected_result_after_tax: '8000', basis: 'recorded_plus_scheduled',
  }), {
    currentNet: 7500, scheduledIncome: 3000, scheduledExpense: 1500,
    estimatedTax: 1000, projectedBeforeTax: 9000, projectedAfterTax: 8000,
    basis: 'recorded_plus_scheduled',
  });
  assert.equal(normalizeForecast({ basis: 'bank_balance' }), null);
});

test('API exposes automation endpoints with expected methods and query names', () => {
  const source = fs.readFileSync(new URL('./api.js', import.meta.url), 'utf8');
  assert.match(source, /recurringOperations:\s*\(\)\s*=>\s*request\('\/api\/recurring-operations'\)/);
  assert.match(source, /addRecurringOperation:\s*\(payload\).*method: 'POST'/);
  assert.match(source, /patchRecurringOperation:\s*\(id, payload\).*method: 'PATCH'/);
  assert.match(source, /deleteRecurringOperation:\s*\(id\).*method: 'DELETE'/);
  assert.match(source, /recurringSuggestions:\s*\(\)\s*=>\s*request\('\/api\/recurring-suggestions'\)/);
  assert.match(source, /insights:\s*\(asOf\)/);
  assert.match(source, /weeklyDigest:\s*\(weekStart\)/);
  assert.match(source, /forecast:\s*\(year, month, asOf\)/);
  assert.match(source, /notificationSettings:\s*\(\)/);
  assert.match(source, /patchNotificationSettings:\s*\(payload\).*method: 'PATCH'/);
});

test('new UI labels forecast as a month result, never a bank balance', () => {
  const reports = fs.readFileSync(new URL('./screens/reports.js', import.meta.url), 'utf8');
  assert.match(reports, /Прогноз результату місяця/);
  assert.match(reports, /не баланс банківського рахунку/i);
  assert.doesNotMatch(reports, /Прогноз банківського балансу/);
});

test('automation UI is opt-in, retryable, accessible and rule-based', () => {
  const automation = fs.readFileSync(new URL('./screens/automation.js', import.meta.url), 'utf8');
  const home = fs.readFileSync(new URL('./screens/home.js', import.meta.url), 'utf8');
  const settings = fs.readFileSync(new URL('./screens/settings.js', import.meta.url), 'utf8');
  const css = fs.readFileSync(new URL('../css/automation.css', import.meta.url), 'utf8');

  assert.match(settings, /data-go="recurring"/);
  assert.match(settings, /data-go="digest"/);
  assert.match(automation, /role="switch"/);
  assert.match(automation, /вимкнена за замовчуванням/i);
  assert.match(automation, /automation-retry/);
  assert.match(home, /normalizeInsights\(await Api\.insights\(\)\)/);
  assert.doesNotMatch(home, /ChatGPT|Claude|Gemini|openai/i);
  assert.match(css, /\.recurring-actions \.btn\s*{[^}]*min-height:\s*var\(--touch-target\)/s);
  assert.match(css, /input\[role="switch"\]\s*{[^}]*height:\s*var\(--touch-target\)/s);
});

test('automation writes cannot restore a stale view and Back keeps a 44px target', () => {
  const automation = fs.readFileSync(new URL('./screens/automation.js', import.meta.url), 'utf8');
  const css = fs.readFileSync(new URL('../css/automation.css', import.meta.url), 'utf8');

  assert.match(automation, /const stillCurrent = \(\) => generation === renderGeneration && root\.dataset\.automationView === 'recurring'/);
  assert.ok((automation.match(/if \(!stillCurrent\(\)\) return;/g) || []).length >= 3);
  assert.match(automation, /root\.dataset\.automationView === 'digest'/);
  assert.match(css, /\.automation-back \.ghost-btn\s*{[^}]*min-width:\s*var\(--touch-target\)[^}]*min-height:\s*var\(--touch-target\)/s);
});
