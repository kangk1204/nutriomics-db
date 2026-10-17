/** Public calculation IDs are stable source IDs; numeric aliases remain compatible. */
export function calculationId(entity: {metadata?: Record<string, unknown>} | null | undefined): string | undefined {
  const metadata = entity?.metadata;
  for (const key of ['calculation_food_id', 'public_food_id', 'core_food_id', 'food_id']) {
    const value = metadata?.[key];
    if (typeof value === 'number' && Number.isInteger(value) && value > 0) return String(value);
    if (typeof value === 'string' && value.trim()) {
      const normalized = value.trim();
      if (/^-?\d+$/.test(normalized) && Number(normalized) <= 0) continue;
      return normalized;
    }
  }
  return undefined;
}
