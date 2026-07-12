import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('./report-data.js', import.meta.url), 'utf8');
const { normalizeMonthlyReport } = await import(`data:text/javascript,${encodeURIComponent(source)}`);

test('normalizes all monthly categories without dropping the remainder', () => {
  const report = normalizeMonthlyReport({
    total_income: 300,
    total_expense: 280,
    transaction_count: 9,
    income_by_category: { Salary: 200, Bonus: 100 },
    expense_by_category: {
      A: 70,
      B: 60,
      C: 50,
      D: 40,
      E: 30,
      F: 20,
      G: 10,
    },
  });

  assert.equal(report.totalIncome, 300);
  assert.equal(report.totalExpense, 280);
  assert.equal(report.transactionCount, 9);
  assert.deepEqual(report.expenseSlices.map(({ name }) => name), ['A', 'B', 'C', 'D', 'E', 'F', 'G']);
  assert.equal(report.expenseSlices.reduce((sum, item) => sum + item.value, 0), report.totalExpense);
  assert.equal(report.incomeSlices.reduce((sum, item) => sum + item.value, 0), report.totalIncome);
});

test('uses category sums when aggregate totals are absent', () => {
  const report = normalizeMonthlyReport({
    income_by_category: { Salary: 125.5 },
    expense_by_category: { Rent: 100, Fees: 25.5 },
  });

  assert.equal(report.totalIncome, 125.5);
  assert.equal(report.totalExpense, 125.5);
});
