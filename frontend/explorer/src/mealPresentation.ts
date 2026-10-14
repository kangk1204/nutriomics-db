export interface MealComponent {
  id: string;
  name: string;
  amount: number | null;
  unit: string | null;
  sources: Record<string, unknown>[];
}

const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;

export function mealPresentation(meal: Record<string, unknown>) {
  const components: MealComponent[] = [];
  for (const value of Array.isArray(meal.components) ? meal.components : []) {
    const row = record(value);
    if (!row) continue;
    components.push({
      id: String(row.component_key ?? row.component_id ?? components.length),
      name: String(row.name ?? row.name_en ?? row.component_key ?? '성분명 미확인'),
      amount: typeof row.amount === 'number' && Number.isFinite(row.amount) ? row.amount : null,
      unit: typeof row.unit === 'string' && row.unit.trim() ? row.unit : null,
      sources: (Array.isArray(row.source_records) ? row.source_records : []).map(record).filter((r): r is Record<string, unknown> => r !== null),
    });
  }
  return {
    components,
    unquantified: (Array.isArray(meal.unquantified) ? meal.unquantified : []).map(record).filter((r): r is Record<string, unknown> => r !== null),
    interpretation: typeof meal.interpretation === 'string' ? meal.interpretation : '',
    status: String(meal.status ?? 'unavailable'),
  };
}
