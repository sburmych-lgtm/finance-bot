import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('./telegram.js', import.meta.url), 'utf8');

async function importWithTelegram(webApp, suffix) {
  globalThis.window = { Telegram: { WebApp: webApp } };
  return import(`data:text/javascript,${encodeURIComponent(source)}#${suffix}`);
}

function stub(versionSupported) {
  const calls = { fullscreen: 0 };
  return {
    calls,
    webApp: {
      ready() {},
      expand() {},
      isVersionAtLeast: (version) => version === '8.0' && versionSupported,
      requestFullscreen: () => { calls.fullscreen += 1; },
    },
  };
}

test('Telegram 6.x never receives the unsupported fullscreen call', async () => {
  const client = stub(false);
  await importWithTelegram(client.webApp, 'telegram-6');
  assert.equal(client.calls.fullscreen, 0);
});

test('Telegram 8.x requests fullscreen when the API is available', async () => {
  const client = stub(true);
  await importWithTelegram(client.webApp, 'telegram-8');
  assert.equal(client.calls.fullscreen, 1);
});
