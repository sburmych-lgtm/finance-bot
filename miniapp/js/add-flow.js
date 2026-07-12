const MONEY_TYPES = new Set(['income', 'expense']);
const CURRENCIES = new Set(['UAH', 'USD', 'EUR']);

function cleanOptionalText(value) {
  if (value == null) return null;
  const text = String(value).trim();
  return text || null;
}

function normalizeTemplate(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const type = String(value.type || value.transaction_type || '').toLowerCase();
  const amount = Number(value.amount);
  const currency = String(value.currency || 'UAH').toUpperCase();
  const category = cleanOptionalText(value.category);
  if (!MONEY_TYPES.has(type) || !Number.isFinite(amount) || amount <= 0) return null;
  if (!CURRENCIES.has(currency) || !category) return null;

  return {
    id: cleanOptionalText(value.id ?? value.key),
    label: cleanOptionalText(value.label),
    type,
    amount,
    currency,
    category,
    subcategory: cleanOptionalText(value.subcategory),
    description: cleanOptionalText(value.description ?? value.comment ?? value.note),
    useCount: Math.max(0, Math.trunc(Number(value.usage_count ?? value.use_count ?? value.count) || 0)),
  };
}

export function normalizeQuickTemplates(payload) {
  const source = Array.isArray(payload)
    ? payload
    : (Array.isArray(payload?.templates) ? payload.templates : []);
  const templates = source.map(normalizeTemplate).filter(Boolean);
  const repeatSource = Array.isArray(payload)
    ? null
    : (payload?.last_operation ?? payload?.repeat_last ?? payload?.last_transaction ?? payload?.last ?? null);

  return {
    templates,
    repeatLast: normalizeTemplate(repeatSource),
  };
}

export function templateToDraft(template, { categories = [], subcategories = [] } = {}) {
  const normalized = normalizeTemplate(template);
  if (!normalized) return null;
  const category = categories.includes(normalized.category) ? normalized.category : null;
  const subcategory = category && subcategories.includes(normalized.subcategory)
    ? normalized.subcategory
    : null;

  return {
    mode: normalized.type,
    amount: String(normalized.amount),
    currency: normalized.currency,
    category,
    subcategory,
    note: normalized.description || category || '',
  };
}

export function createClientRequestId(cryptoImpl = globalThis.crypto) {
  if (typeof cryptoImpl?.randomUUID === 'function') return cryptoImpl.randomUUID();
  if (typeof cryptoImpl?.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    cryptoImpl.getRandomValues(bytes);
    return `ruby-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
  }
  return `ruby-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

export function ensureClientRequestId(currentId, createId = createClientRequestId) {
  return currentId || createId();
}
