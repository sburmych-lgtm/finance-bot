import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('./api.js', import.meta.url), 'utf8');
const testableSource = source.replace(
  "import { Telegram } from './telegram.js';",
  "const Telegram = { initData: 'signed-init-data' };",
);

test('deleteAccount sends an authenticated DELETE with exact confirmation', async () => {
  const calls = [];
  globalThis.window = { __RUBY_API_BASE__: 'https://api.example' };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  const { Api } = await import(`data:text/javascript,${encodeURIComponent(testableSource)}`);
  await Api.deleteAccount('ВИДАЛИТИ');

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://api.example/api/account');
  assert.equal(calls[0].options.method, 'DELETE');
  assert.equal(calls[0].options.headers['X-Telegram-Init-Data'], 'signed-init-data');
  assert.deepEqual(JSON.parse(calls[0].options.body), { confirmation: 'ВИДАЛИТИ' });
});
