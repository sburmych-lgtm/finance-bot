import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('./screens/settings.js', import.meta.url), 'utf8');

test('settings navigation and subcategory expanders are semantic buttons', () => {
  assert.doesNotMatch(source, /<div class="row" data-go=/);
  assert.match(source, /<button type="button" class="row row-action" data-go="expense_cats"/);
  assert.match(source, /<button type="button" class="cat-main" data-toggle-sub=/);
  assert.doesNotMatch(source, /<div class="cat-main" data-toggle-sub=/);
});
