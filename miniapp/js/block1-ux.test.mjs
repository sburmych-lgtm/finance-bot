import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const read = (relative) => readFile(new URL(relative, import.meta.url), 'utf8');

function channel(value) {
  const normalized = value / 255;
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function contrast(foreground, background) {
  const luminance = (hex) => {
    const value = hex.replace('#', '');
    const [r, g, b] = [0, 2, 4].map((offset) => channel(parseInt(value.slice(offset, offset + 2), 16)));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}

function token(source, name) {
  const match = source.match(new RegExp(`--${name}:\\s*(#[0-9A-Fa-f]{6})`));
  assert.ok(match, `missing --${name}`);
  return match[1];
}

test('viewport keeps pinch zoom available', async () => {
  const html = await read('../index.html');
  const viewport = html.match(/<meta name="viewport" content="([^"]+)"/i)?.[1] || '';

  assert.doesNotMatch(viewport, /user-scalable\s*=\s*no/i);
  assert.doesNotMatch(viewport, /maximum-scale\s*=\s*1/i);
});

test('secondary text tokens meet WCAG AA on the darkest card surface', async () => {
  const css = await read('../css/tokens.css');
  const graphite = token(css, 'ruby-graphite');

  assert.ok(contrast(token(css, 'ruby-muted'), graphite) >= 4.5);
  assert.ok(contrast(token(css, 'ruby-dim'), graphite) >= 4.5);
});

test('component system exposes touch, keyboard focus, readable copy and reduced motion contracts', async () => {
  const tokens = await read('../css/tokens.css');
  const css = await read('../css/components.css');

  assert.match(tokens, /--touch-target:\s*44px/);
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion:\s*reduce/);
  assert.match(css, /min-height:\s*var\(--touch-target\)/);
  assert.doesNotMatch(css, /font-size:\s*10px/);
  assert.doesNotMatch(css, /forced-color-adjust:\s*none/);
});

test('reports preserve monthly server aggregation while exposing accessible drill and compact navigation', async () => {
  const source = await read('./screens/reports.js');
  const tabsBlock = source.match(/const TABS = \[([\s\S]*?)\n\];/)?.[1] || '';

  assert.match(source, /Api\.monthlyReport\(state\.year, state\.month\)/);
  assert.doesNotMatch(tabsBlock, /id:\s*'ai'/);
  assert.match(tabsBlock, /label:\s*'Команда'/);
  assert.match(tabsBlock, /label:\s*'Облік'/);
  assert.match(source, /class="reports-tab-shell"/);
  assert.match(source, /role="tablist"/);
  assert.match(source, /role="tab"/);
  assert.match(source, /aria-selected=/);
  assert.match(source, /id="reportAiAction"/);
  assert.match(source, /class="drill-hint"/);
  assert.match(source, /<button type="button" class="legend-item[^`]*data-drill=/);
  assert.match(source, /<button type="button" class="drillable-bar[^`]*data-drill=/);
  assert.match(source, /class="drill-chevron" aria-hidden="true"/);
});

test('onboarding is first-use only, versioned, skippable and finishable', async () => {
  const onboarding = await import('./onboarding.js');
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };

  assert.equal(onboarding.shouldShowOnboarding(storage), true);
  assert.equal(onboarding.ONBOARDING_STEPS.length, 3);
  onboarding.completeOnboarding(storage);
  assert.equal(onboarding.shouldShowOnboarding(storage), false);
  assert.equal(values.get(onboarding.ONBOARDING_STORAGE_KEY), 'complete');

  const source = await read('./onboarding.js');
  assert.match(source, /role="dialog"/);
  assert.match(source, /data-onboarding-skip/);
  assert.match(source, /data-onboarding-finish/);
});

test('empty-state cues are aspirational, contextual and non-interactive', async () => {
  const { emptyStateCue } = await import('./onboarding.js');

  assert.match(emptyStateCue('screen-home'), /операц/i);
  assert.match(emptyStateCue('screen-reports'), /графік|звіт/i);
  assert.match(emptyStateCue('screen-history'), /істор/i);
  assert.equal(emptyStateCue('unknown'), '');
});
