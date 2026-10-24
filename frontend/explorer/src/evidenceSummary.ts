import type { ScientificMatchResult } from './types';

// Multiple matched exposures may refer to the same original analysis contrast.
// These counts describe indexed evidence; they do not count people or assays.
export function summarizeScientificEvidence(matches:ScientificMatchResult|null) {
  if(!matches||matches.rule_version==='not_embedded')return {studies:null,contrasts:null};
  return {
    studies:new Set(matches.observed_evidence.map(e=>e.study_id)).size,
    contrasts:new Set(matches.observed_evidence.flatMap(e=>e.contrasts.map(c=>JSON.stringify([e.study_id,c.series_accession,c.id,c.analysis_version])))).size,
  };
}
