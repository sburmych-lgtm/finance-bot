function finiteAmount(value) {
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : 0;
}

function sortedSlices(value) {
  return Object.entries(value && typeof value === 'object' ? value : {})
    .map(([name, amount]) => ({ name, value: finiteAmount(amount) }))
    .filter(({ value: amount }) => amount > 0)
    .sort((a, b) => b.value - a.value);
}

export function normalizeMonthlyReport(value) {
  const report = value && typeof value === 'object' ? value : {};
  const incomeSlices = sortedSlices(report.income_by_category);
  const expenseSlices = sortedSlices(report.expense_by_category);
  const incomeSum = incomeSlices.reduce((sum, item) => sum + item.value, 0);
  const expenseSum = expenseSlices.reduce((sum, item) => sum + item.value, 0);
  const hasTotalIncome = Number.isFinite(Number(report.total_income));
  const hasTotalExpense = Number.isFinite(Number(report.total_expense));

  return {
    incomeSlices,
    expenseSlices,
    totalIncome: hasTotalIncome ? Number(report.total_income) : incomeSum,
    totalExpense: hasTotalExpense ? Number(report.total_expense) : expenseSum,
    transactionCount: Math.max(0, Math.trunc(finiteAmount(report.transaction_count))),
  };
}
