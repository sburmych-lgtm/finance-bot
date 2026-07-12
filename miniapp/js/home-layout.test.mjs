import test from 'node:test';
import assert from 'node:assert/strict';

import { findDirectSectionHead } from './home-layout.js';


function element(...classes) {
  const classNames = new Set(classes);
  return { classList: { contains: (name) => classNames.has(name) } };
}


test('Home budget anchor ignores a nested stale insights header on rerender', () => {
  const quickActions = element('quick-actions');
  const staleInsights = element('home-insights');
  staleInsights.children = [element('section-head')];
  const recentOperationsHeader = element('section-head');
  const screen = {
    children: [quickActions, staleInsights, recentOperationsHeader],
  };

  assert.equal(findDirectSectionHead(screen), recentOperationsHeader);
});
