"""Scientific negative controls for the Explorer's three independent axes.

Fixtures encode genuinely different studies/materials with deliberately similar
names. A matching label, a source hash, or taxon 9606 must not promote evidence.
"""

from __future__ import annotations

import pytest

from nutrigenomics.explorer_scientific import (
    _dose_card,
    _merge_sample_scopes,
    _run_context,
    compare_context,
    contrast_classification,
    edge_scientific,
    exposure_match_identity,
    feature_match_identity,
    path_scientific,
)


def _edge(
    identifier, subject, target, *, relation="chemical_gene_influence", taxon=None, **context
):
    return {
        "id": identifier,
        "subject": subject,
        "object": target,
        "relation": relation,
        "tier": "curated",
        "review_status": "source_checked",
        "taxon": taxon,
        "context": {"expert_review_status": "pending", **context},
        "source_ids": ["independent-source"],
        "pmids": ["24763279"],
    }


def _node(identifier, kind, label, **metadata):
    return {"id": identifier, "kind": kind, "label": label, "metadata": metadata}


def _dimensions(cards):
    result = {card["dimension"]: card for card in cards}
    assert len(result) == len(cards), "A condition must not silently overwrite another"
    for card in cards:
        assert card["status"] in {"match", "mismatch", "unknown"}
        assert {"input", "study", "reason"} <= set(card)
    return result


def test_missing_context_is_unknown_even_when_study_has_full_context():
    cards = compare_context(
        {},
        {
            "tissue": "whole blood",
            "response_time_h": 6,
            "regimen_duration_days": 14,
            "frequency": "daily",
            "population": "healthy adult men",
        },
    )
    assert cards
    assert all(card["status"] == "unknown" for card in cards)
    assert all(card["input"] is None for card in cards)


@pytest.mark.parametrize(
    ("reference", "observed", "expected"),
    [
        ("PBMC", "whole blood", "mismatch"),
        ("whole blood", "PBMC", "mismatch"),
        ("PBMC", None, "unknown"),
        (None, "whole blood", "unknown"),
        ("whole blood", "whole blood", "match"),
    ],
)
def test_tissue_is_not_made_compatible_by_a_shared_blood_word(reference, observed, expected):
    cards = _dimensions(compare_context({"tissue": reference}, {"tissue": observed}))
    assert cards["tissue"]["status"] == expected
    assert cards["tissue"]["input"] == reference
    assert cards["tissue"]["study"] == observed


def test_one_matching_condition_does_not_hide_a_known_time_mismatch():
    cards = _dimensions(
        compare_context(
            {"tissue": "PBMC", "response_time_h": 6},
            {"tissue": "PBMC", "response_time_h": 2},
        )
    )
    assert cards["tissue"]["status"] == "match"
    time = next(card for name, card in cards.items() if "time" in name)
    assert time["status"] == "mismatch"
    assert time["input"] == 6 and time["study"] == 2


def test_nearly_equal_times_do_not_imply_biological_equivalence():
    cards = _dimensions(compare_context({"response_time_h": 6}, {"response_time_h": 5.99}))
    time = next(card for name, card in cards.items() if "time" in name)
    assert time["status"] == "mismatch"


def test_import_source_check_is_not_expert_approval():
    result = edge_scientific(
        _edge("source-checked", "chemical:mixture", "gene:TNF", taxon="9606"),
        _node("chemical:mixture", "chemical", "Grape-seed MOF"),
        _node("gene:TNF", "gene", "TNF"),
    )
    assert result["study_evidence"]["expert_review_status"] == "pending"
    assert {"mapping_identity", "study_evidence", "conditions", "limitations"} <= set(result)
    assert "confidence" not in result and "score" not in result


def test_name_correspondence_cannot_transfer_a_mixture_quantity():
    edge = _edge(
        "name-candidate",
        "nutrient:flavanols",
        "PubChem:72276",
        relation="candidate_name_correspondence",
        quantity_transfer_allowed=False,
        molecular_identity=False,
    )
    edge["tier"] = "candidate"
    result = edge_scientific(
        edge,
        _node("nutrient:flavanols", "nutrient", "Total flavanols", mixture_or_nutrient_group=True),
        _node("PubChem:72276", "chemical", "Epicatechin"),
    )
    assert result["mapping_identity"]["quantity_transfer_allowed"] is False
    assert "candidate" in result["mapping_identity"]["status"]


def test_an_ordinary_food_name_is_not_the_exact_trial_product():
    seed = _node("food:ordinary-yogurt", "food", "요구르트", core_food_id=20)
    exposure = {
        "id": "N15:yogurt_acute:curation-v1",
        "kind": "product",
        "label": "요구르트 800g 단회",
        "formulation": "trial-specific probiotic yogurt",
        "food_terms": ["요구르트", "yogurt"],
        "component_keys": ["FDC-NUTRIENT:1004"],
    }
    result = exposure_match_identity(seed, exposure, ["FDC-NUTRIENT:1004"])
    assert result["quantity_transfer_allowed"] is False
    assert result["status"] not in {"exact", "exact_product", "same_record", "same_analyte"}


def test_one_analyte_inside_a_mixture_is_not_identity_with_the_whole_mixture():
    seed = _node("FDCNutrient:1272", "nutrient", "DHA", component_key="FDC-NUTRIENT:1272")
    exposure = {
        "id": "N06:fish_oil:curation-v1",
        "kind": "component_group",
        "label": "Fish-oil n-3 mixture",
        "formulation": "EPA and DHA within total n-3 LCPUFA",
        "component_keys": ["FDC-NUTRIENT:1278", "FDC-NUTRIENT:1272"],
        "amount": 3.7,
        "unit": "g",
    }
    result = exposure_match_identity(seed, exposure, ["FDC-NUTRIENT:1272"])
    assert result["status"] != "same_analyte"
    assert result["quantity_transfer_allowed"] is False


def test_multi_study_path_keeps_species_and_tissue_conflicts():
    nodes = [
        _node("food:rice", "food", "Rice"),
        _node("chemical:A", "chemical", "Fixture component"),
        _node("gene:TNF", "gene", "TNF"),
        _node("disease:obesity", "disease", "Obesity"),
    ]
    edges = [
        _edge("composition", "food:rice", "chemical:A", relation="contains_nutrient"),
        _edge(
            "mouse-cell",
            "chemical:A",
            "gene:TNF",
            taxon="10090",
            tissue="liver",
            study_id="mouse-study",
        ),
        _edge(
            "human-blood",
            "gene:TNF",
            "disease:obesity",
            taxon="9606",
            tissue="whole blood",
            study_id="human-study",
        ),
    ]
    result = path_scientific(
        nodes, edges, [node["id"] for node in nodes], [edge["id"] for edge in edges]
    )
    assert result["classification"] == "composed_hypothesis"
    assert result["causal_inference"] is False
    assert [value["edge_id"] for value in result["constituents"]] == [edge["id"] for edge in edges]
    conditions = {value["dimension"]: value for value in result["compatibility"]}
    assert conditions["taxon"]["status"] == "mismatch"
    assert conditions["tissue"]["status"] == "mismatch"
    assert {"10090", "9606"} <= set(conditions["taxon"]["values"])
    assert result["limitations"]


def test_unknown_taxon_is_not_defaulted_to_a_human_path():
    nodes = [_node("chemical:A", "chemical", "A"), _node("gene:TNF", "gene", "TNF")]
    edges = [_edge("unknown-species", "chemical:A", "gene:TNF", taxon=None)]
    result = path_scientific(nodes, edges, [node["id"] for node in nodes], [edges[0]["id"]])
    conditions = {value["dimension"]: value for value in result["compatibility"]}
    assert conditions["taxon"]["status"] == "unknown"
    assert "9606" not in conditions["taxon"]["values"]


@pytest.fixture
def verified_gene_mapping():
    proof = {
        "feature_id": "TNF",
        "feature_type": "gene",
        "series_accession": "GSE56960",
        "source_id": "fixture-HGNC-crosswalk",
        "source_version": "fixture-gene-crosswalk-v1",
        "source_sha256": "a" * 64,
        "namespace": "HGNC_SYMBOL",
        "target_id": "NCBIGene:7124",
        "taxon": "9606",
        "source_taxon": "9606",
        "target_taxon": "9606",
        "verified": True,
    }
    node = _node("NCBIGene:7124", "gene", "TNF", taxon="9606", feature_identifier_mappings=[proof])
    effect = {
        "series_accession": "GSE56960",
        "feature_type": "gene",
        "feature_id": "TNF",
        "analysis_version": "source-analysis-v1",
        "metadata": {"taxon": "9606", "feature_namespace": "HGNC_SYMBOL"},
    }
    return node, effect, proof


def test_explicitly_verified_identifier_crosswalk_is_a_distinct_mapping_axis(verified_gene_mapping):
    node, effect, _ = verified_gene_mapping
    result = feature_match_identity(node, effect)
    assert result["status"] == "source_identifier_match"
    assert result["proof"]["target_id"] == node["id"]
    assert "causal_inference" not in result


def test_legacy_untyped_verified_feature_ids_still_require_identity_verification():
    node = _node("Literature:gene:TNF", "gene", "TNF", verified_feature_ids=["TNF"])
    effect = {"feature_id": "TNF", "feature_type": "gene", "series_accession": "GSE56960"}
    assert feature_match_identity(node, effect)["status"] == "candidate_label"


def test_identifier_proof_for_another_target_gene_cannot_be_reused(verified_gene_mapping):
    node, effect, proof = verified_gene_mapping
    proof["target_id"] = "NCBIGene:7422"
    result = feature_match_identity(node, effect)
    assert result["status"] != "source_identifier_match"


def test_a_known_nonhuman_crosswalk_does_not_prove_human_gene_identity(verified_gene_mapping):
    node, effect, proof = verified_gene_mapping
    proof.update(taxon="10090", source_taxon="10090", target_taxon="10090")
    result = feature_match_identity(node, effect)
    assert result["status"] != "source_identifier_match"


@pytest.mark.parametrize("location", ["proof_source", "node", "effect"])
def test_gene_identity_checks_all_known_taxa_instead_of_the_first_field(
    location, verified_gene_mapping
):
    node, effect, proof = verified_gene_mapping
    if location == "proof_source":
        proof["source_taxon"] = "10090"
    elif location == "node":
        node["metadata"]["taxon"] = "10090"
    else:
        effect["metadata"]["taxon"] = "10090"
    assert feature_match_identity(node, effect)["status"] != "source_identifier_match"


@pytest.mark.parametrize("field", ["source_id", "source_version", "namespace", "verified"])
def test_crosswalk_requires_the_declared_source_and_typed_review(field, verified_gene_mapping):
    node, effect, proof = verified_gene_mapping
    proof.pop(field)
    assert feature_match_identity(node, effect)["status"] == "candidate_label"


@pytest.mark.parametrize("field", ["series_accession", "feature_type", "feature_id"])
def test_crosswalk_for_one_source_feature_cannot_attach_another(field, verified_gene_mapping):
    node, effect, _ = verified_gene_mapping
    effect[field] = "different-fixture-value"
    assert feature_match_identity(node, effect)["status"] == "candidate_label"


def test_pooled_supplement_visits_do_not_become_supplement_effects():
    result = contrast_classification(
        {
            "series_accession": "GSE127530",
            "contrast": "six_hr_vs_fast",
            "design": "randomized_supplement_crossover",
        }
    )
    assert result["classification"] == "pooled_meal_response"
    assert result["component_causal_effect"] is False


@pytest.mark.parametrize("series", ["GSE54325", "GSE54643", "GSE219217"])
def test_public_single_arm_change_keeps_its_analysis_design(series):
    result = contrast_classification(
        {
            "series_accession": series,
            "contrast": "after_vs_before",
            "design": "parent_randomized_trial",
        }
    )
    assert result["classification"] == "within_arm_prepost"
    assert result["original_design"] == "parent_randomized_trial"


@pytest.mark.parametrize(
    ("series", "contrast", "design"),
    [
        ("GSE56960", "post_NormalWeight_500_2h_vs_fast", "randomized_meal_crossover"),
        ("GSE107205", "meddiet_evoo_5y_vs_baseline", "randomized_parent_selected_subset"),
        ("GSE107205", "meddiet_nuts_5y_vs_baseline", "randomized_parent_selected_subset"),
        ("GSE107205", "lowfat_5y_vs_baseline", "randomized_parent_selected_subset"),
    ],
)
def test_parent_randomization_is_not_the_estimand_of_within_arm_contrasts(series, contrast, design):
    result = contrast_classification(
        {
            "series_accession": series,
            "contrast": contrast,
            "design": design,
            "comparison_kind": "within_arm_pre_post",
        }
    )
    assert result["classification"] == "within_arm_prepost"


@pytest.mark.parametrize(
    ("series", "contrast"),
    [
        ("GSE56960", "dose_NormalWeight_1000_vs500_6h_change"),
        ("GSE107205", "evoo_vs_lowfat_5y_change"),
        ("GSE98645", "yogurt_vs_milk_2h_change"),
    ],
)
def test_condition_difference_is_separate_from_component_causality(series, contrast):
    result = contrast_classification(
        {"series_accession": series, "contrast": contrast, "design": "randomized_crossover"}
    )
    assert result["classification"] == "between_condition_change"
    assert result["component_causal_effect"] is False


def test_human_cell_assay_is_not_a_human_ingestion_experiment():
    result = edge_scientific(
        _edge(
            "cell-assay",
            "chemical:MOF",
            "gene:TNF",
            taxon="9606",
            experiment_type="in_vitro_cell_assay",
            tissue="THP-1 human cell line",
        )
    )
    assert result["conditions"]["experiment_type"] == "in_vitro_cell_assay"
    assert result["study_evidence"]["classification"] == "curated_mechanistic_relation"
    assert result["study_evidence"]["component_causal_effect"] is False


def test_daily_14_day_regimen_is_not_equivalent_to_a_single_meal():
    cards = _dimensions(
        compare_context(
            {"regimen_duration_days": 0, "frequency": "single"},
            {"regimen_duration_days": 14, "frequency": "per_day"},
        )
    )
    assert cards["regimen_duration_days"]["status"] == "mismatch"
    assert cards["frequency"]["status"] == "mismatch"


def test_daily_frequency_alias_does_not_infer_an_unprovided_regimen_duration():
    cards = _dimensions(
        compare_context(
            {"frequency": "daily"}, {"frequency": "per_day", "regimen_duration_days": 14}
        )
    )
    assert cards["frequency"]["status"] == "match"
    assert cards["regimen_duration_days"]["status"] == "unknown"


def test_public_health_assertion_keeps_its_known_original_conditions():
    edge = _edge(
        "DASH:systolic_bp",
        "diet:DASH-combination",
        "outcome:systolic_bp",
        relation="randomized_difference",
        taxon="9606",
        original_assertion={
            "study_id": "N13",
            "duration": "8 weeks",
            "comparison": "combination diet versus control diet",
            "effect": -5.5,
            "effect_unit": "mmHg",
            "p_value": None,
            "n_people": None,
            "context_json": '{"study_n_people":459,"p_bound":"<0.001"}',
        },
    )
    edge["tier"] = "human_randomized"
    result = edge_scientific(edge)
    assert result["conditions"]["duration"] == "8 weeks"
    assert result["conditions"]["study_id"] == "N13"
    assert result["study_evidence"]["expert_review_status"] == "pending"


@pytest.mark.parametrize("changed_key", ["series_accession", "analysis_version"])
def test_fdr_family_cannot_be_borrowed_from_another_analysis(changed_key):
    contrast = {"series_accession": "GSE56960", "analysis_version": "old-v1"}
    run = {
        **contrast,
        "status": "completed",
        "metadata": {"multiple_testing": "BH genes x twelve primary contrasts"},
    }
    run[changed_key] = "different-source"
    result = _run_context([run], contrast)
    assert result["multiple_testing"] is None
    assert result["sample_scope"] == {}


def test_missing_fdr_family_remains_unknown_in_a_successfully_completed_run():
    contrast = {"series_accession": "GSE54325", "analysis_version": "source-v1"}
    run = {**contrast, "status": "completed", "metadata": {"completion_marker": "fixture-done"}}
    result = _run_context([run], contrast)
    assert result["multiple_testing"] is None
    assert result["fdr_scope"] is None
    assert result["expert_review_status"] == "pending"


def test_repeated_assays_and_model_people_are_preserved_with_their_original_scope():
    contrast = {"series_accession": "GSE98645", "analysis_version": "acute-studyfamily-v2"}
    counts = {
        "fitted_model_people": 7,
        "fitted_model_assays": 56,
        "public_assays": 77,
        "unique_assays_in_either_analysis": 70,
        "shared_baseline_assays": 14,
        "published_clinical_people": 13,
        "assays_across_runs_must_not_be_summed": True,
    }
    family = "BH jointly across acute 2/4/6h and chronic 2-week gene tests; pathways separate"
    run = {
        **contrast,
        "status": "completed",
        "metadata": {"sample_scope": counts, "multiple_testing": family},
    }
    result = _run_context([run], contrast)
    assert result["sample_scope"] == counts
    assert result["multiple_testing"] == family
    assert result["version"] == "acute-studyfamily-v2"


def test_contrast_scope_complements_the_exact_run_instead_of_replacing_it():
    contrast = {
        "series_accession": "GSE98645",
        "analysis_version": "acute-studyfamily-v2",
        "metadata": {"sample_scope": {"direct_contrast_assays": 28}},
    }
    run = {
        **contrast,
        "metadata": {"sample_scope": {"RNA_people": 7, "public_assays": 77}},
    }
    context = _run_context([run], contrast)
    context.update(
        _merge_sample_scopes(
            [
                *context["sample_scope_sources"],
                {"source": "source_checked_contrast", "values": {"direct_contrast_people": 7}},
            ]
        )
    )
    assert context["sample_scope"] == {
        "RNA_people": 7,
        "public_assays": 77,
        "direct_contrast_assays": 28,
        "direct_contrast_people": 7,
    }
    assert context["sample_scope_conflicts"] == {}
    assert len(context["sample_scope_sources"]) == 3


def test_conflicting_count_in_the_same_scope_is_unknown_and_both_sources_survive():
    context = _merge_sample_scopes(
        [
            {"source": "exact_analysis_run", "values": {"RNA_people": 7, "public_assays": 77}},
            {"source": "source_checked_contrast", "values": {"RNA_people": 13}},
        ]
    )
    assert context["sample_scope"]["RNA_people"] is None
    assert context["sample_scope"]["public_assays"] == 77
    assert context["sample_scope_conflicts"]["RNA_people"] == [7, 13]
    assert context["sample_scope_sources"][0]["values"]["RNA_people"] == 7
    assert context["sample_scope_sources"][1]["values"]["RNA_people"] == 13


def test_ingredient_word_in_a_food_name_does_not_enable_trial_product_mass_comparison():
    seed = _node("FDC:yogurt", "food", "Yogurt, plain, whole milk")
    exposure = {
        "id": "trial-milk",
        "food_terms": ["milk"],
        "food_category_codes": ["acidified_milk"],
    }
    identity = exposure_match_identity(seed, exposure, [])
    assert identity["method"] == "food_term_correspondence"
    card = _dose_card(
        seed,
        exposure,
        identity,
        {"intake_g": 800},
        {
            "amount": 800,
            "unit": "g",
            "frequency": "single",
            "analyte_basis": "whole trial product mass",
        },
    )
    assert card["status"] == "unknown"
    assert card["input"] is None


def test_a_related_yogurt_category_can_compare_nominal_mass_without_quantity_transfer():
    seed = _node("FDC:yogurt", "food", "Yogurt, plain, whole milk")
    exposure = {"id": "trial-yogurt", "food_terms": ["yogurt"], "food_category_codes": ["yogurt"]}
    identity = exposure_match_identity(seed, exposure, [])
    assert identity["method"] == "food_category_correspondence"
    assert identity["quantity_transfer_allowed"] is False
    card = _dose_card(
        seed,
        exposure,
        identity,
        {"intake_g": 150},
        {
            "amount": 800,
            "unit": "g",
            "frequency": "single",
            "analyte_basis": "whole trial product mass",
        },
    )
    assert card["status"] == "mismatch"
