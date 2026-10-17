import type { Core } from 'cytoscape';
export type Kind = 'food' | 'component' | 'gene' | 'health' | 'pathway' | 'cpg' | 'regulatory_region';
export type Tier = 'all' | 'observed' | 'curated' | 'candidate' | 'extracted';
export interface Entity {
  id: string; kind: string; label: string; label_en?: string; aliases?: string[];
  metadata?: Record<string, unknown>; domains?: string[]; degree?: number;
}
export interface Measurement {
  id: string; food?: string; component?: string; component_label?: string;
  amount: number | null; unit?: string; basis?: string; status?: string; source_id?: string;
  assessment?: {eligible: boolean; derivation_method: string; zero_interpretation: string;
    exclusion_reasons: string[]; policy_version: string; source_status: string};
}
export interface EvidenceEdge {
  id: string; subject: string; object: string; relation: string; tier: string;
  review_status?: string; taxon?: string; context?: Record<string, unknown>;
  source_ids?: string[]; pmids?: string[]; domains?: string[]; provenance?: unknown[];
  scientific?: { mapping_identity?: MappingIdentity; study_evidence?: StudyEvidence; conditions?: ApplicabilityCondition[] | Record<string, unknown>; limitations?: string[] };
}
export interface NodeDetail extends Entity {
  measurements?: Measurement[]; studies?: Record<string, unknown>[];
  effects?: Record<string, unknown>[]; gaps?: string[]; provenance?: unknown[];
  methylation_annotations?: Record<string, unknown>[]; total_methylation_annotations?: number;
  scientific_matches?: ScientificMatchResult;
  regulatory_evidence?: WorkbenchEvidence[]; total_regulatory_evidence?: number;
  verified_identifier_links?: WorkbenchEvidence[];
}
export interface WorkbenchEvidence {
  id: string; subject_id: string; object_id: string; layer: string; relation: string;
  study_id?: string; tissue?: string; organism?: string; review_status?: string;
  expert_review_status?: string; effect?: number | null; effect_unit?: string;
  p_value?: number | null; p_value_original?: string; q_value?: number | null;
  ci_low?: number | null; ci_high?: number | null; hypothesis_family?: string;
  n_people?: number | null; n_samples?: number | null; export_allowed?: boolean;
  source_ref?: string; source?: Record<string, unknown>; context?: Record<string, unknown>;
  metadata?: Record<string, unknown>; study?: Record<string, unknown>;
}
export interface EvidencePage { release_id: string; items: WorkbenchEvidence[]; total: number; page: number; page_size: number; status?: string; gaps?: string[] }
export interface Locus { evidence_id: string; subject_id: string; object_id: string; chromosome: string; start: number; end: number; layer: string; tissue?: string; coordinate_convention?: string; source?: Record<string, unknown> }
export interface ReferenceContext {
  tissue?: string | null; response_time_h?: number | null; regimen_duration_days?: number | null;
  frequency?: 'single' | 'daily' | null; population?: string | null; intake_g?: number | null;
}
export interface ScientificContext { seed_id: string; intake_g?: number; reference_context: ReferenceContext; gene_node_id?: string }
export interface MappingIdentity {
  status?: string; label?: string; method?: string; quantity_transfer_allowed?: boolean; note?: string;
}
export interface StudyEvidence {
  classification?: string; label?: string; allocation?: string; population?: string;
  original_design?: string; expert_review_status?: string; species?: string; tissue?: string;
}
export interface ApplicabilityCondition {
  dimension: string; status: 'match' | 'mismatch' | 'unknown'; input?: unknown; study?: unknown; reason: string;
  contrast_id?: string;
}
export interface ScientificContrast {
  id: string; series_accession?: string; contrast?: string; analysis_version?: string;
  comparison?: string; design_classification?: string; label?: string;
  n_people?: number | null; n_samples?: number | null; n_people_scope?: string; n_samples_scope?: string;
  effects?: Record<string, unknown>[]; effect_total?: number | null; effects_status?: string; source_effects_status?: string; analysis_context?: Record<string, unknown>;
  source_url?: string; all_results_url?: string; applicability?: ApplicabilityCondition[]; effect_preview?: string;
  matched_exposure_role?: string; comparison_exposures?: Record<string, unknown>[]; exposure_role_note?: string;
  source_effect_total?: number | null; gene_filter_applied?: boolean;
}
export interface ScientificEvidence {
  id: string; study_id: string; study_label?: string; evidence_group?: string;
  exposure: { id?: string; node_id?: string; label?: string; kind?: string; formulation?: string; amount?: number | null; unit?: string; duration_days?: number | null; frequency?: string; dose_basis?: string; actual_product_description?: string };
  mapping_identity: MappingIdentity; study_evidence: StudyEvidence;
  applicability: ApplicabilityCondition[]; contrasts: ScientificContrast[]; clinical_results: Record<string, unknown>[];
  provenance: Record<string, unknown>[]; limitations: string[]; result_status?: string; export_allowed?: boolean;
}
export interface ScientificMatchResult {
  release_id: string; rule_version: string; seed_id: string; reference_context: ReferenceContext;
  observed_evidence: ScientificEvidence[]; evidence_gaps: string[]; note?: string;
}
export interface GraphPath {
  id: string; node_ids: string[]; edge_ids: string[]; label: string; weakest_link?: string;
  scientific?: { classification?: string; causal_inference?: boolean; constituents?: unknown[]; compatibility?: { dimension: string; status: string; values: unknown[]; reason: string }[]; limitations?: string[] };
}
export interface GraphData {
  nodes: Entity[]; edges: EvidenceEdge[]; paths: GraphPath[]; gaps: string[];
  truncated?: boolean; omitted?: Record<string, number>; release_id: string;
}
export interface Meta {
  release_id: string; created_at: string; counts: Record<string, unknown>;
  domains?: { id: string; label: string }[]; scope?: unknown; gaps?: string[];
}
export interface Filters { domain: string; tier: Tier; include_extracted: boolean; scientific_context?: ScientificContext }
export interface ExportRequest {
  format: 'html' | 'json' | 'csv'; release_id: string; node_ids: string[];
  edge_ids: string[]; positions: Record<string, { x: number; y: number }>;
  filters: Filters;
}
export interface Snapshot extends GraphData {
  created_at?: string; filters?: Filters; meta?: Meta; offline: boolean;
  node_details?: Record<string, NodeDetail>; edge_details?: Record<string, EvidenceEdge>;
  positions?: Record<string, { x: number; y: number }>;
}
declare global {
  interface Window {
    __NUTRIOMICS_SNAPSHOT__?: Snapshot;
    __NUTRIOMICS_CY__?: Core;
    __NUTRIOMICS_POSITIONS__?: Record<string, { x: number; y: number }>;
    __NUTRIOMICS_DATA_RECEIVED_AT__?: number;
    __NUTRIOMICS_RENDER_TIMING__?: {layout_ms: number; render_to_frame_ms: number; data_to_frame_ms: number|null; preloaded_before_render_ms: number|null; displayed_nodes: number; total_nodes: number};
  }
}
