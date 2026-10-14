import type { ApplicabilityCondition, ScientificContrast, ScientificEvidence } from './types';

export function contrastConditions(exposure?: ScientificEvidence, contrast?: ScientificContrast): ApplicabilityCondition[] {
  if (!exposure) return [];
  const inputDimensions = new Set(['food_formulation', 'dose']);
  return [
    ...exposure.applicability.filter(item => inputDimensions.has(item.dimension)),
    ...(contrast?.applicability || exposure.applicability.filter(item => !inputDimensions.has(item.dimension))),
  ];
}
