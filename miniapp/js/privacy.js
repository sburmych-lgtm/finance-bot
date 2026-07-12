export const ACCOUNT_DELETE_CONFIRMATION = 'ВИДАЛИТИ';

export function isAccountDeleteConfirmation(value) {
  return typeof value === 'string' && value === ACCOUNT_DELETE_CONFIRMATION;
}
