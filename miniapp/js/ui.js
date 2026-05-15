/* Ruby Finance — small UI helpers */

export function fmtMoney(value, currency = 'UAH') {
  const sign = value < 0 ? '−' : (value > 0 ? '+' : '');
  const abs = Math.abs(Number(value) || 0);
  const formatted = abs.toLocaleString('uk-UA', { maximumFractionDigits: 2 });
  const symbol = currency === 'UAH' ? '₴' : currency === 'USD' ? '$' : currency === 'EUR' ? '€' : currency;
  return `${sign}${formatted} ${symbol}`;
}

export function fmtDate(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  if (isNaN(d)) return dateStr;
  return d.toLocaleDateString('uk-UA', { day: 'numeric', month: 'short' });
}

export function toast(text, duration = 1800) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), duration);
}

export function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v === false || v === null || v === undefined) {/* skip */}
    else if (v === true) node.setAttribute(k, '');
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}
