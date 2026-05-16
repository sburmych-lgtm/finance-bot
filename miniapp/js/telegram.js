/* Ruby Finance — Telegram WebApp SDK wrapper */

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

export const Telegram = {
  ready() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      // Try fullscreen (Bot API 8.0+); silently ignore on older clients.
      if (typeof tg.requestFullscreen === 'function') {
        try { tg.requestFullscreen(); } catch (_) {}
      }
      // Disable vertical swipes so dragging inside numpad / scroll doesn't close the app.
      if (typeof tg.disableVerticalSwipes === 'function') {
        try { tg.disableVerticalSwipes(); } catch (_) {}
      }
      // Ruby Finance is dark-only — paint the Telegram chrome ruby-ink so
      // there is no light flash regardless of the user's Telegram theme.
      if (typeof tg.setHeaderColor === 'function') {
        try { tg.setHeaderColor('#0A0608'); } catch (_) {
          try { tg.setHeaderColor('bg_color'); } catch (_) {}
        }
      }
      if (typeof tg.setBackgroundColor === 'function') {
        try { tg.setBackgroundColor('#0A0608'); } catch (_) {}
      }
      if (typeof tg.setBottomBarColor === 'function') {
        try { tg.setBottomBarColor('#0A0608'); } catch (_) {}
      }
    } catch (e) {
      console.warn('Telegram.ready failed', e);
    }
  },

  get initData()       { return tg?.initData || ''; },
  get initDataUnsafe() { return tg?.initDataUnsafe || null; },
  get user()           { return tg?.initDataUnsafe?.user || null; },
  get colorScheme()    { return tg?.colorScheme || 'dark'; },
  get themeParams()    { return tg?.themeParams || {}; },
  get available()      { return Boolean(tg); },

  haptic(type = 'light') {
    if (!tg?.HapticFeedback) return;
    try {
      if (type === 'success' || type === 'error' || type === 'warning') {
        tg.HapticFeedback.notificationOccurred(type);
      } else if (type === 'selection') {
        tg.HapticFeedback.selectionChanged();
      } else {
        tg.HapticFeedback.impactOccurred(type); // light | medium | heavy | rigid | soft
      }
    } catch (_) {}
  },

  showMainButton(text, onClick) {
    if (!tg?.MainButton) return;
    try {
      tg.MainButton.setText(text);
      tg.MainButton.onClick(onClick);
      tg.MainButton.show();
    } catch (_) {}
  },

  hideMainButton() {
    try { tg?.MainButton?.hide(); } catch (_) {}
  },

  showBackButton(onClick) {
    if (!tg?.BackButton) return;
    try {
      tg.BackButton.onClick(onClick);
      tg.BackButton.show();
    } catch (_) {}
  },

  hideBackButton() {
    try { tg?.BackButton?.hide(); } catch (_) {}
  },

  onThemeChange(cb) {
    try { tg?.onEvent('themeChanged', cb); } catch (_) {}
  },

  close() {
    try { tg?.close(); } catch (_) {}
  },
};

// Auto-init on import
Telegram.ready();
