/* Ruby Finance — Telegram WebApp SDK wrapper */

const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;

export const Telegram = {
  ready() {
    if (!tg) return;
    try {
      tg.ready();
      tg.expand();
      // Telegram exposes some future methods on older SDK shells but logs an
      // unsupported-method error when they are invoked. Check the negotiated
      // WebApp version as well as method presence.
      const supportsFullscreen = (
        typeof tg.isVersionAtLeast === 'function'
        && tg.isVersionAtLeast('8.0')
      );
      if (supportsFullscreen && typeof tg.requestFullscreen === 'function') {
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
      // Fullscreen mode overlays Telegram's own Close / ⋯ controls on top of the
      // app, covering our header. Push the header below them using Telegram's
      // safe-area insets (device notch) + content-safe-area insets (TG controls).
      const applyInsets = () => {
        try {
          const sa = tg.safeAreaInset || {};
          const csa = tg.contentSafeAreaInset || {};
          const top = (Number(sa.top) || 0) + (Number(csa.top) || 0);
          if (top > 0) {
            document.documentElement.style.setProperty('--tg-top-inset', `${top}px`);
          }
        } catch (_) {}
      };
      applyInsets();
      // Insets can arrive slightly after the fullscreen transition — re-apply on
      // the relevant events (and once shortly after) so the header settles right.
      ['safeAreaChanged', 'contentSafeAreaChanged', 'fullscreenChanged'].forEach((ev) => {
        try { tg.onEvent(ev, applyInsets); } catch (_) {}
      });
      setTimeout(applyInsets, 300);
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

  showPopup(params, callback) {
    if (!tg?.showPopup) return false;
    try {
      tg.showPopup(params, callback);
      return true;
    } catch (_) {
      return false;
    }
  },

  close() {
    try { tg?.close(); } catch (_) {}
  },
};

// Auto-init on import
Telegram.ready();
