/* Ruby Finance — API client */

import { Telegram } from './telegram.js';

const API_BASE = (window.__RUBY_API_BASE__ || '').replace(/\/$/, '');

async function request(path, { method = 'GET', body } = {}) {
  const url = `${API_BASE}${path}`;
  const headers = {
    'Content-Type': 'application/json',
    'X-Telegram-Init-Data': Telegram.initData || '',
  };
  const res = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let payload = {};
    try { payload = (await res.json()) || {}; } catch (_) {}
    const error = new Error(payload.detail || `HTTP ${res.status}`);
    error.code = payload.code || `HTTP_${res.status}`;
    error.status = res.status;
    if (error.code === 'INIT_DATA_EXPIRED') {
      window.dispatchEvent(new CustomEvent('ruby:auth-expired', {
        detail: { message: error.message },
      }));
    }
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}

export const Api = {
  // Profile
  me:              ()                    => request('/api/me'),

  // Low-level escape hatch so screens can hand-craft query strings.
  // Returns parsed JSON (or null on 204) and throws Error(detail) on !ok.
  _request:        (path, opts)          => request(path, opts),

  // Balance + transactions
  getBalance:      (year, month)         => request(`/api/balance?year=${year}&month=${month}`),
  listTransactions:(limit = 15)          => request(`/api/transactions?limit=${limit}`),
  quickTemplates:  ()                    => request('/api/quick-templates'),
  addTransaction:  (payload)             => request('/api/transactions',           { method: 'POST', body: payload }),
  deleteTransaction:(id)                 => request(`/api/transactions/${id}`,     { method: 'DELETE' }),

  // Reports
  monthlyReport:   (year, month)         => request(`/api/reports/monthly?year=${year}&month=${month}`),
  employeeReport:  (year, month)         => request(`/api/reports/employees?year=${year}&month=${month}`),
  taxReport:       (year, month)         => request(`/api/reports/tax?year=${year}&month=${month}`),
  accountingReport:(year, month)         => request(`/api/reports/accounting?year=${year}&month=${month}`),
  timeReport:      (year, month)         => request(`/api/reports/time?year=${year}&month=${month}`),

  // Categories
  categories:      ()                    => request('/api/categories'),
  categoriesFull:  ()                    => request('/api/categories/full'),
  addCategory:     (payload)             => request('/api/categories',                       { method: 'POST',   body: payload }),
  patchCategory:   (type, name, payload) => request(`/api/categories/${type}/${encodeURIComponent(name)}`, { method: 'PATCH', body: payload }),
  deleteCategory:  (type, name)          => request(`/api/categories/${type}/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  // Subcategories (nested under a category)
  addSubcategory:    (type, cat, name)   => request(`/api/categories/${type}/${encodeURIComponent(cat)}/subcategories`, { method: 'POST', body: { name } }),
  deleteSubcategory: (type, cat, sub)    => request(`/api/categories/${type}/${encodeURIComponent(cat)}/subcategories/${encodeURIComponent(sub)}`, { method: 'DELETE' }),

  // Drill-down: one category's transactions aggregated by subcategory
  categoryBreakdown: (type, category, period, year, month) => {
    const p = new URLSearchParams({ type, category });
    if (period) p.set('period', period);
    if (year)   p.set('year', String(year));
    if (month)  p.set('month', String(month));
    return request(`/api/reports/category-breakdown?${p.toString()}`);
  },

  // Employees
  employees:       ()                    => request('/api/employees'),
  addEmployee:     (name)                => request('/api/employees',                        { method: 'POST',   body: { name } }),
  deleteEmployee:  (name)                => request(`/api/employees/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  // Time categories
  timeCategories:  ()                    => request('/api/time-categories'),
  addTimeCategory: (name, emoji)         => request('/api/time-categories',                  { method: 'POST',   body: { name, emoji } }),
  deleteTimeCategory:(name)              => request(`/api/time-categories/${encodeURIComponent(name)}`, { method: 'DELETE' }),

  // Time tracks
  listTimeTracks:  (year, month, limit)  => request(`/api/time-tracks?year=${year}&month=${month}${limit ? `&limit=${limit}` : ''}`),
  addTimeTrack:    (payload)             => request('/api/time-tracks',                      { method: 'POST',   body: payload }),
  deleteTimeTrack: (id)                  => request(`/api/time-tracks/${id}`,                { method: 'DELETE' }),

  // Settings
  settings:        ()                    => request('/api/settings'),
  patchTax:        (payload)             => request('/api/settings/tax',                     { method: 'PATCH',  body: payload }),
  resetSettings:   ()                    => request('/api/settings',                         { method: 'DELETE' }),
  deleteAccount:   (confirmation)        => request('/api/account',                          { method: 'DELETE', body: { confirmation } }),
  exchangeRates:   ()                    => request('/api/exchange-rates'),
};
