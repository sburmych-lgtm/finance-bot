import { normalizePaymentSource } from './block2-ui.js';

const TYPES = new Set(['income', 'expense']);
const CURRENCIES = new Set(['UAH', 'USD', 'EUR']);
const FREQUENCIES = new Set(['daily', 'weekly', 'monthly', 'yearly']);
const INSIGHT_KINDS = new Set([
  'budget_warning',
  'weekly_category_change',
  'income_concentration',
]);

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function positiveNumber(value) {
  const number = finiteNumber(value, NaN);
  return number > 0 ? number : null;
}

function positiveInteger(value, fallback = null) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function optionalText(value) {
  const text = cleanText(value);
  return text || null;
}

function isoDate(value) {
  const text = cleanText(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  const parsed = new Date(`${text}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== text
    ? null
    : text;
}

function normalizeRecurring(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  const id = positiveInteger(row.id);
  const type = TYPES.has(row.type) ? row.type : null;
  const amount = positiveNumber(row.amount);
  const currency = CURRENCIES.has(row.currency) ? row.currency : null;
  const amountUah = positiveNumber(row.amount_uah);
  const category = cleanText(row.category);
  const frequency = FREQUENCIES.has(row.frequency) ? row.frequency : null;
  const interval = positiveInteger(row.interval);
  const startDate = isoDate(row.start_date);
  const nextDueDate = isoDate(row.next_due_date);
  if (!id || !type || !amount || !currency || !amountUah || !category || !frequency || !interval || !startDate || !nextDueDate) {
    return null;
  }
  return {
    id,
    type,
    amount,
    currency,
    amountUah,
    category,
    subcategory: optionalText(row.subcategory),
    description: cleanText(row.description),
    paymentSource: normalizePaymentSource(row.payment_source),
    frequency,
    interval,
    startDate,
    nextDueDate,
    autoCreate: row.auto_create === true,
    active: row.active === true,
  };
}

export function normalizeRecurringOperations(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.operations;
  return Array.isArray(rows) ? rows.map(normalizeRecurring).filter(Boolean) : [];
}

export function buildRecurringPatch(original, draft) {
  if (!original || !draft || typeof draft !== 'object') return {};
  const mappings = [
    ['type', 'type'],
    ['amount', 'amount'],
    ['currency', 'currency'],
    ['category', 'category'],
    ['subcategory', 'subcategory'],
    ['description', 'description'],
    ['payment_source', 'paymentSource'],
    ['frequency', 'frequency'],
    ['interval', 'interval'],
    ['start_date', 'startDate'],
    ['auto_create', 'autoCreate'],
  ];
  return Object.fromEntries(mappings.flatMap(([payloadKey, originalKey]) => {
    const next = draft[payloadKey];
    const previous = original[originalKey];
    return Object.is(next, previous) ? [] : [[payloadKey, next]];
  }));
}

function frequencyNoun(frequency, interval) {
  const lastTwo = interval % 100;
  const last = interval % 10;
  const plural = lastTwo >= 11 && lastTwo <= 14 ? 5 : last;
  const forms = {
    daily: ['день', 'дні', 'днів'],
    weekly: ['тиждень', 'тижні', 'тижнів'],
    monthly: ['місяць', 'місяці', 'місяців'],
    yearly: ['рік', 'роки', 'років'],
  }[frequency] || ['період', 'періоди', 'періодів'];
  return forms[plural === 1 ? 0 : plural >= 2 && plural <= 4 ? 1 : 2];
}

export function recurrenceLabel(frequency, interval = 1) {
  const count = positiveInteger(interval, 1);
  if (count === 1) {
    return {
      daily: 'Щодня',
      weekly: 'Щотижня',
      monthly: 'Щомісяця',
      yearly: 'Щороку',
    }[frequency] || 'Регулярно';
  }
  return `Кожні ${count} ${frequencyNoun(frequency, count)}`;
}

function normalizeSuggestion(row) {
  if (!row || typeof row !== 'object' || Array.isArray(row)) return null;
  const type = TYPES.has(row.type) ? row.type : null;
  const category = cleanText(row.category);
  const subcategory = optionalText(row.subcategory);
  const amount = positiveNumber(row.amount);
  const currency = CURRENCIES.has(row.currency) ? row.currency : null;
  const amountUah = positiveNumber(row.amount_uah);
  const frequency = FREQUENCIES.has(row.frequency) ? row.frequency : null;
  const occurrences = positiveInteger(row.occurrences);
  const lastDate = isoDate(row.last_date);
  const nextDate = isoDate(row.next_date);
  if (!type || !category || !amount || !currency || !amountUah || !frequency || !occurrences || !lastDate || !nextDate) return null;
  return {
    type,
    category,
    subcategory,
    amount,
    currency,
    amountUah,
    paymentSource: normalizePaymentSource(row.payment_source),
    description: cleanText(row.description),
    frequency,
    occurrences,
    lastDate,
    nextDate,
  };
}

export function normalizeRecurringSuggestions(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.suggestions;
  return Array.isArray(rows) ? rows.map(normalizeSuggestion).filter(Boolean) : [];
}

function normalizeInsight(row) {
  if (!row || typeof row !== 'object' || !INSIGHT_KINDS.has(row.kind)) return null;
  const category = cleanText(row.category);
  const percent = finiteNumber(row.percent, NaN);
  if (!category || !Number.isFinite(percent)) return null;
  if (row.kind === 'budget_warning') {
    return {
      kind: row.kind,
      category,
      percent,
      spent: Math.max(0, finiteNumber(row.spent_uah)),
      limit: Math.max(0, finiteNumber(row.limit_uah)),
    };
  }
  if (row.kind === 'weekly_category_change') {
    return {
      kind: row.kind,
      category,
      percent,
      current: Math.max(0, finiteNumber(row.current_uah)),
      previous: Math.max(0, finiteNumber(row.previous_uah)),
    };
  }
  return {
    kind: row.kind,
    category,
    percent,
    amount: Math.max(0, finiteNumber(row.amount_uah)),
  };
}

export function normalizeInsights(payload) {
  const rows = Array.isArray(payload) ? payload : payload?.insights;
  return Array.isArray(rows) ? rows.map(normalizeInsight).filter(Boolean) : [];
}

function money(value) {
  return `${finiteNumber(value).toLocaleString('uk-UA', { maximumFractionDigits: 2 })} ₴`;
}

export function insightPresentation(insight) {
  if (insight?.kind === 'budget_warning') {
    return {
      tone: insight.percent > 100 ? 'danger' : 'warning',
      icon: '◎',
      title: `${Math.round(insight.percent)}% бюджету «${insight.category}»`,
      body: `${money(insight.spent)} із ${money(insight.limit)} вже використано цього місяця.`,
    };
  }
  if (insight?.kind === 'weekly_category_change') {
    const signed = `${insight.percent > 0 ? '+' : ''}${Math.round(insight.percent)}%`;
    return {
      tone: insight.percent > 0 ? 'warning' : 'positive',
      icon: insight.percent > 0 ? '↗' : '↘',
      title: `${signed} на «${insight.category}» цього тижня`,
      body: `Зараз ${money(insight.current)}, попереднього тижня — ${money(insight.previous)}.`,
    };
  }
  if (insight?.kind === 'income_concentration') {
    return {
      tone: 'neutral',
      icon: '◈',
      title: `${Math.round(insight.percent)}% доходу з одного джерела`,
      body: `«${insight.category}» принесло ${money(insight.amount)} цього місяця.`,
    };
  }
  return null;
}

export function normalizeNotificationSettings(payload) {
  return { weeklyDigestEnabled: payload?.weekly_digest_enabled === true };
}

export function normalizeDigest(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const periodStart = isoDate(payload.period_start);
  const periodEnd = isoDate(payload.period_end);
  if (!periodStart || !periodEnd) return null;
  return {
    periodStart,
    periodEnd,
    totalIncome: finiteNumber(payload.total_income),
    totalExpense: finiteNumber(payload.total_expense),
    net: finiteNumber(payload.net),
    transactionCount: Math.max(0, Math.trunc(finiteNumber(payload.transaction_count))),
    topExpenseCategory: optionalText(payload.top_expense_category),
    topExpenseAmount: Math.max(0, finiteNumber(payload.top_expense_amount)),
  };
}

export function normalizeForecast(payload) {
  if (!payload || payload.basis !== 'recorded_plus_scheduled') return null;
  return {
    currentNet: finiteNumber(payload.current_net),
    scheduledIncome: finiteNumber(payload.scheduled_income),
    scheduledExpense: finiteNumber(payload.scheduled_expense),
    estimatedTax: finiteNumber(payload.estimated_tax),
    projectedBeforeTax: finiteNumber(payload.projected_result_before_tax),
    projectedAfterTax: finiteNumber(payload.projected_result_after_tax),
    basis: payload.basis,
  };
}
