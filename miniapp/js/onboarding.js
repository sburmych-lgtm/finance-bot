/* First-use onboarding and aspirational empty-state cues. */

export const ONBOARDING_STORAGE_KEY = 'ruby-finance:onboarding:v1';

export const ONBOARDING_STEPS = Object.freeze([
  Object.freeze({
    symbol: 'R',
    title: 'Фінанси без зайвої складності',
    copy: 'Додавайте доходи, витрати й час за кілька натискань. Категорії та підрозділи завжди можна налаштувати під себе.',
  }),
  Object.freeze({
    symbol: 'R',
    title: 'Повна картина місяця',
    copy: 'Звіти рахуються за всіма операціями. Натисніть категорію в легенді, щоб побачити деталі по підрозділах.',
  }),
  Object.freeze({
    symbol: '%',
    title: 'Податки й дані під контролем',
    copy: 'Оберіть свою групу ФОП, а Ruby покаже орієнтовний розрахунок. Усі записи ізольовані за вашим Telegram ID.',
  }),
]);

const EMPTY_STATE_CUES = Object.freeze({
  'screen-home': 'Після першої операції тут з\'явиться ваш фінансовий ритм.',
  'screen-reports': 'Додайте операції — графік і звіт зберуться автоматично.',
  'screen-history': 'Тут з\'явиться хронологія вашої фінансової історії.',
});

function readStorage(storage) {
  try {
    return storage?.getItem(ONBOARDING_STORAGE_KEY) || null;
  } catch (_) {
    return null;
  }
}

export function shouldShowOnboarding(storage) {
  return readStorage(storage) !== 'complete';
}

export function completeOnboarding(storage) {
  try {
    storage?.setItem(ONBOARDING_STORAGE_KEY, 'complete');
    return true;
  } catch (_) {
    return false;
  }
}

export function emptyStateCue(screenId) {
  return EMPTY_STATE_CUES[screenId] || '';
}

export function enhanceEmptyStates(root) {
  if (!root?.querySelectorAll) return;
  const doc = root.ownerDocument || root;
  root.querySelectorAll('.empty-state:not([data-aspirational-enhanced])').forEach((empty) => {
    empty.dataset.aspirationalEnhanced = 'true';
    if ((empty.textContent || '').toLocaleLowerCase('uk').includes('помил')) return;
    const cue = emptyStateCue(empty.closest('.screen')?.id || '');
    if (!cue) return;
    const preview = doc.createElement('p');
    preview.className = 'empty-state-cue';
    preview.textContent = cue;
    empty.appendChild(preview);
  });
}

function browserStorage() {
  try {
    return window.localStorage;
  } catch (_) {
    return null;
  }
}

export function initOnboarding({ doc = document, storage = browserStorage() } = {}) {
  if (!doc?.body || !shouldShowOnboarding(storage) || doc.querySelector('[data-onboarding]')) {
    return null;
  }

  let stepIndex = 0;
  const previousFocus = doc.activeElement;
  const layer = doc.createElement('div');
  layer.className = 'onboarding-layer';
  layer.dataset.onboarding = 'true';
  layer.innerHTML = `
    <section class="onboarding-panel" role="dialog" aria-modal="true" aria-labelledby="onboardingTitle" aria-describedby="onboardingCopy">
      <div class="onboarding-topline">
        <div class="onboarding-progress" aria-live="polite"></div>
        <button type="button" class="onboarding-skip" data-onboarding-skip>Пропустити</button>
      </div>
      <div class="onboarding-symbol" aria-hidden="true"></div>
      <h2 class="onboarding-title" id="onboardingTitle"></h2>
      <p class="onboarding-copy" id="onboardingCopy"></p>
      <div class="onboarding-dots" aria-hidden="true"></div>
      <div class="onboarding-actions">
        <button type="button" class="btn btn-secondary" data-onboarding-back>Назад</button>
        <button type="button" class="btn btn-primary" data-onboarding-next>Далі</button>
        <button type="button" class="btn btn-primary" data-onboarding-finish>Почати роботу</button>
      </div>
    </section>`;

  const progress = layer.querySelector('.onboarding-progress');
  const symbol = layer.querySelector('.onboarding-symbol');
  const title = layer.querySelector('.onboarding-title');
  const copy = layer.querySelector('.onboarding-copy');
  const dots = layer.querySelector('.onboarding-dots');
  const back = layer.querySelector('[data-onboarding-back]');
  const next = layer.querySelector('[data-onboarding-next]');
  const finish = layer.querySelector('[data-onboarding-finish]');

  const render = () => {
    const step = ONBOARDING_STEPS[stepIndex];
    progress.textContent = `Крок ${stepIndex + 1} з ${ONBOARDING_STEPS.length}`;
    symbol.textContent = step.symbol;
    title.textContent = step.title;
    copy.textContent = step.copy;
    dots.innerHTML = ONBOARDING_STEPS.map((_, index) =>
      `<span class="onboarding-dot ${index === stepIndex ? 'active' : ''}"></span>`
    ).join('');
    back.hidden = stepIndex === 0;
    next.hidden = stepIndex === ONBOARDING_STEPS.length - 1;
    finish.hidden = stepIndex !== ONBOARDING_STEPS.length - 1;
  };

  const close = () => {
    completeOnboarding(storage);
    doc.removeEventListener('keydown', onKeydown);
    layer.remove();
    previousFocus?.focus?.();
  };

  const onKeydown = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== 'Tab') return;
    const controls = [...layer.querySelectorAll('button:not([hidden])')];
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && doc.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && doc.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  layer.querySelector('[data-onboarding-skip]').addEventListener('click', close);
  finish.addEventListener('click', close);
  next.addEventListener('click', () => {
    stepIndex = Math.min(ONBOARDING_STEPS.length - 1, stepIndex + 1);
    render();
    next.focus();
  });
  back.addEventListener('click', () => {
    stepIndex = Math.max(0, stepIndex - 1);
    render();
    back.focus();
  });
  doc.addEventListener('keydown', onKeydown);
  doc.body.appendChild(layer);
  render();
  layer.querySelector('[data-onboarding-skip]').focus();
  return layer;
}

export function observeEmptyStates(doc = document) {
  enhanceEmptyStates(doc);
  if (typeof MutationObserver === 'undefined' || !doc?.body) return null;
  const observer = new MutationObserver(() => enhanceEmptyStates(doc));
  observer.observe(doc.body, { childList: true, subtree: true });
  return observer;
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  const start = () => {
    observeEmptyStates(document);
    window.setTimeout(() => initOnboarding(), 250);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
}
