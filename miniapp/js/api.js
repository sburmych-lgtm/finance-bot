/* Ruby Finance — API client.
   All requests carry raw initData in X-Telegram-Init-Data header; backend validates HMAC-SHA256. */

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
    let detail = '';
    try { detail = (await res.json())?.detail || ''; } catch (_) {}
    throw new Error(detail || `HTTP ${res.status}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const Api = {
  me:              ()                    => request('/api/me'),
  getBalance:      (year, month)         => request(`/api/balance?year=${year}&month=${month}`),
  listTransactions:(limit = 15)          => request(`/api/transactions?limit=${limit}`),
  addTransaction:  (payload)             => request('/api/transactions', { method: 'POST', body: payload }),
  deleteTransaction:(id)                 => request(`/api/transactions/${id}`, { method: 'DELETE' }),
  monthlyReport:   (year, month)         => request(`/api/reports/monthly?year=${year}&month=${month}`),
  settings:        ()                    => request('/api/settings'),
  categories:      ()                    => request('/api/categories'),
  exchangeRates:   ()                    => request('/api/exchange-rates'),
};
