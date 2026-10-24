"""Synthetic software fixtures; never released as empirical benchmark evidence."""

import json
import io
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from nutri_literature import benchmarks
from nutri_literature.db import connect, digest, dumps, initialize
from nutri_literature.evaluation import _sets
from nutri_literature.promotion import operational_gate, promotion_status
from nutri_literature.public_corpora import (
    CATALOG, _brat_members, _ebm_members, parse_bioc_json, parse_bioscope, parse_pubtator, prepare_public,
)
from nutri_literature.settings import PROJECT, Settings


def _settings(tmp_path):
    settings = Settings(tmp_path / "runtime", PROJECT / "config/topics.json")
    initialize(settings)
    return settings


def _native_source(pmid="9000001"):
    text = "Vitamin D decreases VDR expression. Another chemical binds OTHER."
    left, right = text.index("Vitamin D"), text.index("VDR")
    other, chemical = text.index("OTHER"), text.index("chemical")
    raw = (f"{pmid}|t|{text}\n{pmid}|a|\n"
           f"{pmid}\t{left}\t{left+9}\tVitamin D\tChemicalEntity\tD123\n"
           f"{pmid}\t{right}\t{right+3}\tVDR\tGeneOrGeneProduct\t99\n"
           f"{pmid}\t{chemical}\t{chemical+8}\tchemical\tChemicalEntity\tD456\n"
           f"{pmid}\t{other}\t{other+5}\tOTHER\tGeneOrGeneProduct\t88\n"
           f"{pmid}\tNegative_Correlation\tD123\t99\tNovel\n"
           f"{pmid}\tD123\t99\tSubject:D123\n"
           f"{pmid}\tConversion\tD123\t88\tNovel\n")
    return raw, text + " "


def _mock_engine(monkeypatch):
    from nutri_literature import dictionaries, engines
    dictionary = SimpleNamespace(version="fixture-dictionary", manifest={"fallback": True})
    provenance = {"engine_version": "heuristic-v2", "rules_version": "fixture-rules",
                  "dictionary_version": dictionary.version, "dictionary_manifest": dictionary.manifest,
                  "parser": {"available": True, "mode": "production", "name": "en_core_sci_sm",
                             "version": "0.5.4", "spacy_version": "3.7.5", "scispacy_version": "0.6.2",
                             "model_sha256": "a" * 64},
                  "crosswalk": benchmarks._crosswalk(None, dictionary)[1],
                  "projection_sha256": digest(dumps(benchmarks.PROJECTION)),
                  "evaluator_source_hashes": benchmarks.evaluator_source_hashes()}

    def fake_provenance(settings, engine, crosswalk_path=None):
        return dictionary, {**provenance, "engine_version": engine}

    def fake_extract(text, sections, dictionaries, metadata=None, engine="heuristic-v1"):
        metadata = metadata or {}
        entities = metadata.get("_gold_entities", [])
        chemicals = [entity for entity in entities if entity["normalized_id"] == "D123"]
        genes = [entity for entity in entities if entity["normalized_id"] == "99"]
        events = []
        if chemicals and genes:
            events = [{"subject": "D123", "object": "99", "predicate": "decreases",
                       "subject_mention": chemicals[0], "object_mention": genes[0],
                       "start": 0, "end": len(text), "quote": text,
                       "polarity": "positive", "speculative": False, "status": "candidate"}]
        return {"mentions": entities, "events": events,
                "manifest": {"oracle": "_gold_entities" in metadata}, "oracle_entities_used": bool(entities)}

    monkeypatch.setattr(benchmarks, "_provenance", fake_provenance)
    monkeypatch.setattr(dictionaries, "load_dictionaries", lambda settings, engine=None: dictionary)
    monkeypatch.setattr(engines, "extract_engine", fake_extract)
    return provenance


def test_pubtator_preserves_native_types_direction_and_exact_offsets():
    raw, text = _native_source()
    papers = parse_pubtator(raw)
    assert papers[0]["text"] == text
    assert papers[0]["relations"][0]["predicate"] == "Negative_Correlation"
    assert papers[0]["relations"][0]["subject"] == "D123"
    assert papers[0]["relations"][1]["predicate"] == "Conversion"
    assert papers[0]["entities"][1]["normalized_id"] == "99"
    with pytest.raises(ValueError, match="Unicode"):
        parse_pubtator(raw.replace("\t20\t23\tVDR", "\t0\t23\tVDR"))


def test_bioc_json_keeps_document_offsets_and_not_shifted_passage_offsets():
    payload = {"documents": [{"id": "fixture", "passages": [
        {"offset": 0, "text": "Gene", "annotations": []},
        {"offset": 5, "text": "VDR", "annotations": [
            {"id": "a", "infons": {"type": "GeneOrGeneProduct", "identifier": "99"},
             "locations": [{"offset": 5, "length": 3}], "text": "VDR"}]}], "relations": []}]}
    result = parse_bioc_json(dumps(payload))[0]
    assert result["text"] == "Gene VDR" and result["entities"][0]["start"] == 5


def test_manual_semantic_evaluation_uses_only_gold_annotated_fields():
    text = "Vitamin D may decrease VDR expression."
    relation = {"subject": "D123", "predicate": "decreases", "object": "99",
                "quote": text, "start": 0, "end": len(text)}
    document = {"pmid": "fixture", "text": text, "entities": [], "relations": [relation]}
    prediction = {"entities": [], "relations": [{**relation, "polarity": "negated", "speculative": True}]}
    assert _sets(document, document)["relation"] == _sets(document, prediction)["relation"]
    document["relations"] = [{**relation, "polarity": "positive", "speculative": True,
                               "arm_id": "arm1", "timepoint": "12 weeks"}]
    prediction["relations"] = [{**relation, "payload": {"polarity": "positive", "speculative": False},
                                  "conditions": {"arm_id": "arm2", "timepoint": "12 weeks"}}]
    assert not _sets(document, document)["relation"] & _sets(document, prediction)["relation"]
    prediction["relations"][0]["payload"]["speculative"] = True
    prediction["relations"][0]["conditions"]["arm_id"] = "arm1"
    assert _sets(document, document)["relation"] == _sets(document, prediction)["relation"]


def test_public_oracle_keeps_unsupported_and_cross_sentence_false_negatives(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _mock_engine(monkeypatch)
    raw, _ = _native_source()
    source = tmp_path / "dev.pubtator"
    source.write_text(raw, encoding="utf-8")
    prepared = prepare_public(settings, "bioredirect", source_path=source)
    run = benchmarks.predict_public(settings, prepared["inputs_path"], mode="gold_entities")
    report = benchmarks.evaluate_public(settings, prepared["gold_path"], run["prediction_run"])
    assert report["metrics"]["relation"]["tp"] == 1
    assert report["metrics"]["relation"]["fn"] == 1
    assert report["metrics"]["relation"]["precision"] == 1.0
    assert report["metrics"]["relation"]["recall"] == .5
    assert report["metrics"]["direction"]["f1"] == 1.0
    assert report["relation_coverage"]["unsupported_types"] == ["Conversion"]
    assert report["relation_coverage"]["gold_same_sentence"] == 1
    assert report["normalization"]["oracle_not_detector_result"] is True
    assert report["claims"]["nutrition_accuracy"] == "unmeasured"
    assert report["provenance_coverage"]["fraction"] == 1.0


def test_end_to_end_prediction_never_opens_gold(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _mock_engine(monkeypatch)
    source = tmp_path / "dev.pubtator"
    source.write_text(_native_source()[0], encoding="utf-8")
    prepared = prepare_public(settings, "biored", source_path=source)
    Path(prepared["gold_path"]).rename(Path(prepared["gold_path"]).with_name("sealed-gold.json"))
    run = benchmarks.predict_public(settings, prepared["inputs_path"], mode="end_to_end")
    assert run["status"] == "predictions_only_no_relation_gold_read"


def test_test_attempt_requires_frozen_engine_and_cannot_repeat(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _mock_engine(monkeypatch)
    source = tmp_path / "test.pubtator"
    source.write_text(_native_source()[0], encoding="utf-8")
    prepared = prepare_public(settings, "biored", split="test", source_path=source)
    with pytest.raises(ValueError, match="frozen engine"):
        benchmarks.predict_public(settings, prepared["inputs_path"], mode="gold_entities")
    frozen = benchmarks.freeze_benchmark(settings, prepared["inputs_path"])
    run = benchmarks.predict_public(settings, prepared["inputs_path"], freeze_path=frozen["freeze_path"],
                                    mode="gold_entities")
    assert run["freeze_id"] == frozen["id"]
    with pytest.raises(FileExistsError):
        benchmarks.predict_public(settings, prepared["inputs_path"], freeze_path=frozen["freeze_path"],
                                  mode="gold_entities")


def test_changed_public_inputs_or_manifest_cannot_replace_import_receipt(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _mock_engine(monkeypatch)
    source = tmp_path / "dev.pubtator"
    source.write_text(_native_source()[0], encoding="utf-8")
    prepared = prepare_public(settings, "biored", source_path=source)
    path = Path(prepared["inputs_path"])
    content = json.loads(path.read_bytes())
    content["documents"][0]["text"] = "modified"
    path.write_text(dumps(content), encoding="utf-8")
    manifest_path = Path(prepared["manifest_path"])
    manifest = json.loads(manifest_path.read_bytes())
    manifest["inputs_sha256"] = digest(path.read_bytes())
    manifest_path.write_text(dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="durable imported-source"):
        benchmarks.predict_public(settings, path)


def test_paired_bootstrap_samples_papers_preserves_denominators_and_nulls():
    baseline = {"p1": {"relation": {"tp": 1, "fp": 9, "fn": 1}},
                "p2": {"relation": {"tp": 1, "fp": 0, "fn": 1}}}
    candidate = {"p1": {"relation": {"tp": 2, "fp": 0, "fn": 0}},
                 "p2": {"relation": {"tp": 1, "fp": 0, "fn": 1}}}
    result = benchmarks.paired_bootstrap(["p1", "p2"], baseline, candidate)
    assert result["iterations"] == 2000 and result["papers"] == 2
    assert result["metrics"]["precision"]["delta"] == pytest.approx(1 - 2 / 11)
    assert result["metrics"]["recall"]["delta"] == .25
    assert result == benchmarks.paired_bootstrap(["p1", "p2"], baseline, candidate)
    empty = {"p1": {"relation": {"tp": 0, "fp": 0, "fn": 1}}}
    result = benchmarks.paired_bootstrap(["p1"], empty, empty)
    assert result["metrics"]["precision"]["delta"] is None
    assert result["metrics"]["precision"]["valid_draws"] == 0
    with pytest.raises(ValueError, match="same nonempty"):
        benchmarks.paired_bootstrap(["other"], baseline, candidate)


def _bioscope_source():
    records = []
    for i in range(10):
        records.append(f'<Document id="{100000+i}"><sentence><sentence_text>'
                       f'<xcope id="n{i}">VDR is <cue type="negation" ref="n{i}">not</cue> induced.</xcope>'
                       '</sentence_text></sentence></Document>')
    return "<corpus>" + "".join(records) + "</corpus>"


def test_bioscope_scope_xml_preserves_cues_and_research_partition_only():
    papers = parse_bioscope(_bioscope_source())
    assert papers[0]["text"] == "VDR is not induced."
    assert papers[0]["scopes"][0]["kind"] == "negation"
    assert papers[0]["cues"][0]["text"] == "not"
    with pytest.raises(ValueError, match="biological"):
        parse_bioscope(_bioscope_source(), "clinical")


def test_nonofficial_holdout_is_grouped_pinned_label_independent_and_nonoverlapping(tmp_path):
    settings = _settings(tmp_path)
    source = tmp_path / "bioscope.xml"
    source.write_text(_bioscope_source(), encoding="utf-8")
    dev = prepare_public(settings, "bioscope", source_path=source, license_reference="fixture-license")
    test = prepare_public(settings, "bioscope", "test", source_path=source, license_reference="fixture-license")
    a = json.loads(Path(dev["inputs_path"]).read_bytes())
    b = json.loads(Path(test["inputs_path"]).read_bytes())
    assert a["manifest"]["official_split"] is False
    assert a["manifest"]["preregistered_split"] is True
    assert a["manifest"]["complete_partition"] is True
    assert a["manifest"]["partition_sha256"] == b["manifest"]["partition_sha256"]
    assert len(a["documents"]) == 8 and len(b["documents"]) == 2
    assert not {d["pmid"] for d in a["documents"]} & {d["pmid"] for d in b["documents"]}
    with connect(settings) as con:
        assert con.execute("SELECT count(*) FROM audit WHERE kind='public_partition_predeclared'").fetchone()[0] == 1


def test_brat_discontinuous_outcomes_do_not_fill_unannotated_gap():
    text = "pain and blood pressure"
    members = {"123.txt": text.encode(), "123.ann": b"T1\tOutcome 0 4;9 23\tpain blood pressure\n"}
    result = _brat_members(members)[0]
    assert [span["text"] for span in result["annotated_spans"]] == ["pain", "blood pressure"]


def test_ebm_aggregated_labels_and_original_offsets():
    members = {"ebm/documents/123.text": b"Adults received vitamin D.",
               "ebm/documents/123.tokens": b"Adults received vitamin D .",
               "ebm/annotations/aggregated/starting_spans/participants/train/123.AGGREGATED.ann": b"1 0 0 0 0",
               "ebm/annotations/aggregated/starting_spans/interventions/train/123.AGGREGATED.ann": b"0 0 1 1 0"}
    result = _ebm_members(members, "development")[0]
    assert [span["text"] for span in result["annotated_spans"]] == ["Adults", "vitamin D"]


def test_scope_token_evaluation_keeps_abstract_fulltext_dimensions():
    text = "VDR is not induced."
    gold = {"pmid": "fixture", "text": text, "scopes": [
        {"kind": "negation", "start": 0, "end": len(text), "text": text}]}
    observed = {"scopes": [{"kind": "negation", "start": 7, "end": len(text), "text": text[7:]}]}
    expected, predicted = benchmarks._span_sets(gold, observed, "scopes", "kind")
    assert len(expected) == 4 and len(predicted) == 2
    assert benchmarks._metrics(expected, predicted)["recall"] == .5


def _all_task_predictions(document):
    text = document["text"]
    span = {"start": 0, "end": len(text), "text": text}
    return {"entities": [{**span, "kind": "gene", "normalized_id": "HGNC:fixture"}],
            "events": [],
            "relations": [{"entity1": "fixture-chemical", "entity2": "fixture-gene",
                           "predicate": "Association", "subject": "fixture-chemical"}],
            "scopes": [{**span, "kind": "negation"}],
            "annotated_spans": [{**span, "label": label} for label in ["participants", "interventions", "outcomes"]]}


def test_native_biored_zero_gold_is_negative_only_for_annotated_tasks():
    document = parse_pubtator("901001|t|VDR is induced.\n901001|a|\n")[0]
    counts = benchmarks._paper_counts(document, _all_task_predictions(document), {}, "end_to_end",
                                     annotated_tasks=CATALOG["biored"]["tasks"])
    assert counts["gene_detection"]["fp"] == counts["relation"]["fp"] == 1
    assert counts["normalization"]["normalized_predictions"] == 1
    assert counts["relation_gold_total"] == 0
    assert not {"direction", "scopes", "scopes_exact", "annotated_spans"} & counts.keys()
    assert counts["provenance_valid"] == counts["provenance_total"] == 5


def test_native_bioscope_zero_gold_counts_scope_fp_without_inventing_gene_labels():
    document = parse_bioscope('<corpus><Document id="901002"><sentence><sentence_text>'
                             'VDR is induced.</sentence_text></sentence></Document></corpus>')[0]
    counts = benchmarks._paper_counts(document, _all_task_predictions(document), {}, "end_to_end",
                                     annotated_tasks=CATALOG["bioscope"]["tasks"])
    assert counts["scopes:negation"]["fp"] == 3
    assert counts["scopes:speculation"]["tp"] == counts["scopes:speculation"]["fp"] == 0
    assert counts["scopes:speculation"]["f1"] is None
    assert counts["scopes_exact"]["fp"] == 1
    assert not {"gene_detection", "relation", "direction", "normalization", "annotated_spans"} & counts.keys()
    assert counts["provenance_valid"] == counts["provenance_total"] == 5


def test_native_evidenceoutcomes_only_outcomes_enter_pico_aggregate():
    document = _brat_members({"901003.txt": b"pain", "901003.ann": b"T1\tOutcome 0 4\tpain\n"})[0]
    counts = benchmarks._paper_counts(document, _all_task_predictions(document), {}, "end_to_end",
                                     annotated_tasks=CATALOG["evidenceoutcomes"]["tasks"])
    assert counts["annotated_spans"] == counts["annotated_spans:outcomes"]
    assert counts["annotated_spans"]["tp"] == 1 and counts["annotated_spans"]["fp"] == 0
    assert not {"gene_detection", "relation", "normalization", "scopes",
                "annotated_spans:participants", "annotated_spans:interventions"} & counts.keys()
    assert counts["provenance_valid"] == counts["provenance_total"] == 5


def test_native_ebm_report_and_comparison_exclude_unannotated_endpoints(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _mock_engine(monkeypatch)
    from nutri_literature import engines

    def fake_extract(text, sections, dictionaries, metadata=None, engine=None):
        values = _all_task_predictions({"text": text})
        return {"mentions": values["entities"], "events": [], "benchmark_scopes": values["scopes"],
                "annotated_spans": values["annotated_spans"], "manifest": {}}

    monkeypatch.setattr(engines, "extract_engine", fake_extract)
    members = {"ebm/documents/901004.text": b"Adults received vitamin D.",
               "ebm/documents/901004.tokens": b"Adults received vitamin D ."}
    for label in ["participants", "interventions", "outcomes"]:
        members[f"ebm/annotations/aggregated/starting_spans/{label}/train/901004.AGGREGATED.ann"] = b"0 0 0 0 0"
    members.update({"ebm/documents/901005.text": b"Adults received vitamin D.",
                    "ebm/documents/901005.tokens": b"Adults received vitamin D .",
                    "ebm/annotations/aggregated/starting_spans/participants/train/901005.AGGREGATED.ann": b"0 0 0 0 0"})
    source = tmp_path / "ebm.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name, raw in members.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            archive.addfile(entry, io.BytesIO(raw))
    prepared = prepare_public(settings, "ebmnlp", source_path=source)
    run = benchmarks.predict_public(settings, prepared["inputs_path"])
    report = benchmarks.evaluate_public(settings, prepared["gold_path"], run["prediction_run"])
    endpoints = {"annotated_spans", "annotated_spans:participants", "annotated_spans:interventions", "annotated_spans:outcomes"}
    assert set(report["metrics"]) == endpoints
    assert report["metrics"]["annotated_spans"]["fp"] == 16  # Only four available paper-task pairs.
    assert report["metrics"]["annotated_spans:outcomes"]["fp"] == 4  # Missing second file is not negative.
    assert report["annotation_applicability"]["annotated_documents_by_task"] == {
        "participants": 2, "interventions": 1, "outcomes": 1,
    }
    assert report["metrics"]["annotated_spans:outcomes"]["f1_ci95"]["papers"] == 1
    assert report["normalization"] is report["relation_by_type"] is report["relation_coverage"] is None
    assert report["scope_by_source"] == {}
    assert "scopes" in report["annotation_applicability"]["not_evaluated_endpoints"]
    assert report["provenance_coverage"] == {"valid": 10, "total": 10, "fraction": 1.0}
    comparison = benchmarks.compare_public(settings, prepared["gold_path"], run["prediction_run"], run["prediction_run"])
    assert set(comparison["bootstrap"]) == endpoints
    assert comparison["bootstrap"]["annotated_spans:participants"]["papers"] == 2
    assert comparison["bootstrap"]["annotated_spans:outcomes"]["papers"] == 1

    # An unscored scope still must correspond to the immutable source.
    altered = json.loads(Path(run["prediction_run"]).read_bytes())
    altered["id"] = "fixture-invalid-unscored-scope"
    altered["documents"][0]["scopes"][0]["text"] = "incorrect source"
    invalid_path = tmp_path / "invalid-run.json"
    invalid_path.write_text(dumps(altered), encoding="utf-8")
    benchmarks._receipt(settings, "public_benchmark_prediction", altered["id"], altered)
    with pytest.raises(ValueError, match="offsets"):
        benchmarks.evaluate_public(settings, prepared["gold_path"], invalid_path)


def test_annotation_applicability_requires_pinned_native_schema_not_positive_presence():
    for corpus, spec in CATALOG.items():
        coverage = benchmarks._annotation_applicability({"corpus": corpus, "tasks": spec["tasks"]})
        assert set(coverage["tasks"]) == set(spec["tasks"])
        assert not set(coverage["evaluated_endpoints"]) & set(coverage["not_evaluated_endpoints"])
    with pytest.raises(ValueError, match="coverage"):
        benchmarks._annotation_applicability({"corpus": "ebmnlp", "tasks": ["gene_detection"]})


def test_no_gold_no_operation_no_promotion_even_if_user_passed_flags_are_true(tmp_path):
    settings = _settings(tmp_path)
    result = promotion_status([{"passed": True}], {"passed": True}, {"passed": True}, settings=settings)
    assert result["passed"] is False and result["default_engine"] == "heuristic-v1"
    assert result["nutrition_accuracy"] == "unmeasured_pending_domain_gold"
    assert "unverified_public_measurement" in result["reasons"]


def test_old_v1_soak_receipt_and_noncontinuous_v2_samples_do_not_pass(tmp_path):
    settings = _settings(tmp_path)
    contract = json.loads((PROJECT / "config/benchmark-acceptance-v2.json").read_bytes())
    result = operational_gate({"status": "passed", "elapsed_hours": 200}, {}, contract, settings)
    assert result["passed"] is False
    assert "operational_engine_provenance_mismatch" in result["reasons"]


def test_normalized_json_cannot_claim_complete_official_partition(tmp_path):
    settings = _settings(tmp_path)
    paper = parse_pubtator(_native_source()[0])[0]
    source = tmp_path / "fixture.json"
    source.write_text(dumps({"schema_version": "literature-public-source-v1", "corpus": "biored",
                             "split": "development", "documents": [paper]}), encoding="utf-8")
    prepared = prepare_public(settings, "biored", source_path=source)
    manifest = json.loads(Path(prepared["manifest_path"]).read_bytes())
    assert manifest["official_split"] is False and manifest["complete_partition"] is False


def test_compare_reuses_audit_pinned_measured_reports_without_repeating_bootstrap(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    _mock_engine(monkeypatch)
    source = tmp_path / "dev.pubtator"
    source.write_text(_native_source()[0], encoding="utf-8")
    prepared = prepare_public(settings, "biored", source_path=source)
    run = benchmarks.predict_public(settings, prepared["inputs_path"], mode="gold_entities")
    first = benchmarks.evaluate_public(settings, prepared["gold_path"], run["prediction_run"])
    monkeypatch.setattr(benchmarks, "_paper_intervals", lambda *a, **kw: pytest.fail("cached report recomputed"))
    cached = benchmarks.evaluate_public(settings, prepared["gold_path"], run["prediction_run"], reuse=True)
    assert cached["id"] == first["id"]
    with connect(settings) as con:
        assert con.execute("SELECT count(*) FROM audit WHERE kind='public_benchmark_evaluation'").fetchone()[0] == 1


def test_cue_linked_scope_matching_cannot_hide_wrong_cue_by_unioning_tokens():
    text = "VDR was not induced and may change."
    scope = {"kind": "negation", "start": 0, "end": len(text), "text": text}
    document = {"pmid": "fixture", "text": text,
                "scopes": [{**scope, "cue_spans": [{"start": 8, "end": 11}]}]}
    observed = {"scopes": [{**scope, "cue_start": 24, "cue_end": 27}]}
    expected, predicted = benchmarks._span_sets(document, observed, "scopes", "kind")
    assert not expected & predicted


def test_operational_probe_hash_matches_actual_flat_diagnostics_schema_and_rejects_mutation(tmp_path):
    from nutri_literature.promotion import _provenance_identity

    settings = _settings(tmp_path)
    parser = {"available": True, "mode": "production", "name": "en_core_sci_sm", "version": "0.5.4",
              "spacy_version": "3.7.5", "scispacy_version": "0.6.2", "model_sha256": "a" * 64}
    provenance = {"engine_version": "heuristic-v2", "rules_version": "fixture-rules",
                  "dictionary_version": "fixture-dictionary", "parser": parser}
    identity = _provenance_identity(provenance)
    samples = []
    last = datetime.now(timezone.utc).replace(microsecond=0)
    for moment in [last - timedelta(minutes=5), last]:
        detail = {"services": {"api": True, "ui": True}, "worker_alive": True}
        full = {"at": moment.isoformat(), "engine": "heuristic-v2", "ok": True,
                "provenance": identity, "detail": detail}
        sha = digest(dumps(full))
        with connect(settings) as con:
            con.execute("INSERT INTO engine_health VALUES(?,?,?,?,?,?)", (
                full["at"], "heuristic-v2", 1, dumps(identity), dumps(detail), sha,
            ))
        samples.append({"at": full["at"], "ok": True, "source": "durable_health_snapshot",
                        **identity, "snapshot_sha256": sha})
    record = {"schema_version": "literature-engine-soak-v1", "id": "fixture-soak", "engine": "heuristic-v2",
              "provenance": identity, "started_at": samples[0]["at"], "completed_at": samples[-1]["at"],
              "samples": samples, "checks": {}}
    record["receipt_sha256"] = digest(dumps(record))
    benchmarks._receipt(settings, "engine_soak_receipt", record["id"], record)
    contract = json.loads((PROJECT / "config/benchmark-acceptance-v2.json").read_bytes())
    actual = operational_gate(record, provenance, contract, settings)
    assert actual["passed"] is False and "seven_day_v2_operation_pending" in actual["reasons"]
    assert "health_sample_not_bound_to_durable_probe" not in actual["reasons"]
    with connect(settings) as con:
        con.execute("UPDATE engine_health SET detail='{}'")
    changed = operational_gate(record, provenance, contract, settings)
    assert "health_sample_not_bound_to_durable_probe" in changed["reasons"]
