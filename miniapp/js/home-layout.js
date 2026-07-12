export function findDirectSectionHead(screen) {
  if (!screen?.children) return null;
  return Array.from(screen.children).find((child) =>
    child?.classList?.contains('section-head')
  ) || null;
}

