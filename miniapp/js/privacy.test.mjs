import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('./privacy.js', import.meta.url), 'utf8');
const privacy = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test('account deletion requires the exact Ukrainian confirmation phrase', () => {
  assert.equal(privacy.ACCOUNT_DELETE_CONFIRMATION, 'ВИДАЛИТИ');
  assert.equal(privacy.isAccountDeleteConfirmation('ВИДАЛИТИ'), true);

  for (const value of [undefined, null, '', 'видалити', 'DELETE', ' ВИДАЛИТИ ', 1]) {
    assert.equal(privacy.isAccountDeleteConfirmation(value), false);
  }
});
