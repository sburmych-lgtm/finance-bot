const PAYMENT_SOURCES = Object.freeze([
  Object.freeze({ value: 'cash', label: 'Готівка', icon: '₴' }),
  Object.freeze({ value: 'card', label: 'Картка', icon: '▭' }),
  Object.freeze({ value: 'transfer', label: 'Переказ', icon: '→' }),
  Object.freeze({ value: 'other', label: 'Інше', icon: '•' }),
]);

const PAYMENT_SOURCE_VALUES = new Set(PAYMENT_SOURCES.map(({ value }) => value));

export function normalizePaymentSource(value) {
  return typeof value === 'string' && PAYMENT_SOURCE_VALUES.has(value) ? value : null;
}

export function paymentSourceOptions() {
  return PAYMENT_SOURCES.map((option) => ({ ...option }));
}

export function paymentSourceLabel(value) {
  const normalized = normalizePaymentSource(value);
  return PAYMENT_SOURCES.find(({ value: candidate }) => candidate === normalized)?.label || 'Не вказано';
}

function finiteMoney(value) {
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : 0;
}

function normalizeBudget(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const type = value.type === 'income' ? 'income' : value.type === 'expense' ? 'expense' : null;
  const category = typeof value.category === 'string' ? value.category.trim() : '';
  const monthlyLimit = finiteMoney(value.monthly_limit_uah);
  if (!type || !category || monthlyLimit <= 0) return null;
  return {
    type,
    category,
    monthlyLimit,
    spent: Math.max(0, finiteMoney(value.spent_uah)),
    remaining: finiteMoney(value.remaining_uah),
    progressPercent: Math.max(0, finiteMoney(value.progress_percent)),
    isExceeded: value.is_exceeded === true,
  };
}

export function normalizeBudgetResponse(payload) {
  const source = Array.isArray(payload) ? payload : payload?.budgets;
  return Array.isArray(source) ? source.map(normalizeBudget).filter(Boolean) : [];
}

const SOURCE_REPORT_ORDER = Object.freeze(['cash', 'card', 'transfer', 'other', 'unclassified']);

export function normalizePaymentSourceBreakdown(payload, type = 'expense') {
  const field = type === 'income' ? 'income_by_payment_source' : 'expense_by_payment_source';
  const values = payload?.[field] && typeof payload[field] === 'object' ? payload[field] : {};
  return SOURCE_REPORT_ORDER.map((source) => ({
    source: source === 'unclassified' ? null : source,
    label: paymentSourceLabel(source === 'unclassified' ? null : source),
    value: Math.max(0, finiteMoney(values[source])),
  })).filter(({ value }) => value > 0);
}

export function budgetTone(budget) {
  if (budget?.isExceeded || Number(budget?.progressPercent) > 100) return 'exceeded';
  if (Number(budget?.progressPercent) >= 80) return 'warning';
  return 'normal';
}
