import json
from pathlib import Path

import pytest

from nutri_literature.db import connect, digest, dumps, initialize
from nutri_literature.evaluation import _metrics, evaluate, predict_gold, prepare_gold
from nutri_literature.extraction import RULES_VERSION
from nutri_literature.settings import PROJECT, Settings


def populated(tmp_path, count=24):
    settings = Settings(tmp_path / "runtime", PROJECT / "config/topics.json")
    initialize(settings)
    text = "Vitamin D inhibits VDR."
    gene_start = text.index("VDR")
    with connect(settings) as con:
        topic_rows = con.execute("SELECT topic_id,query_version FROM topics ORDER BY topic_id").fetchall()
        topics = [row[0] for row in topic_rows]
        versions = {row[0]: row[1] for row in topic_rows}
        for index in range(count):
            pid, sid, eid = index + 1, f"source-{index}", f"extract-{index}"
            con.execute(
                "INSERT INTO papers(paper_id,pmid,title,abstract,doi,created_at,updated_at,current_source_id) "
                "VALUES(?,?,?,?,?,'2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00',?)",
                (
                    pid,
                    str(12345678 + index),
                    "Synthetic evaluation fixture",
                    text,
                    f"10.fixture/{index}",
                    sid,
                ),
            )
            topic = topics[index % len(topics)]
            con.execute("INSERT INTO paper_topics VALUES(?,?,?)", (pid, topic, versions[topic]))
            con.execute(
                "INSERT INTO sources VALUES(?,?,'pubmed',?,?,?,?,'[]',?,NULL,?)",
                (
                    sid,
                    pid,
                    digest(text),
                    digest(text),
                    "synthetic.xml",
                    text,
                    "https://example.invalid/fixture",
                    "2026-10-01T00:00:00+00:00",
                ),
            )
            con.execute(
                "INSERT INTO extractions VALUES(?,?,?,?,'fixture-engine','fixture-dictionary',?,?,1)",
                (eid, pid, sid, digest(text), dumps({"rules_version": RULES_VERSION}), "2026-10-01T00:00:00+00:00"),
            )
            con.execute(
                "INSERT INTO mentions VALUES(?,?,?,'gene','VDR','HGNC:12679',?,?,'candidate','{}')",
                (f"mention-{index}", eid, pid, gene_start, gene_start + 3),
            )
            con.execute(
                "INSERT INTO assertions VALUES(?,?,?,?,'CHEBI:27300','inhibits','HGNC:12679',?,0,?,'fixture','candidate','{}')",
                (f"assertion-{index}", eid, pid, f"fp-{index}", text, len(text)),
            )
    return settings


def review(gold_path, predictions_path, split=None):
    gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
    predictions = {
        d["pmid"]: d for d in json.loads(Path(predictions_path).read_text(encoding="utf-8"))["documents"]
    }
    # Explicitly synthetic reviewer fixture, not real performance evidence.
    for document in gold["documents"]:
        if split is not None and document["split"] != split:
            continue
        document.update(
            review_status="reviewed", reviewers=["fixture-reviewer-1", "fixture-reviewer-2"], adjudicated=True
        )
        document["entities"] = predictions[document["pmid"]]["entities"]
        document["relations"] = predictions[document["pmid"]]["relations"]
    Path(gold_path).write_text(dumps(gold), encoding="utf-8")
    return gold


def test_gold_has_balanced_document_splits_strata_and_pending_manual_labels(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = json.loads(Path(prepared["gold_path"]).read_text(encoding="utf-8"))
    assert prepared["development"] == prepared["heldout"] == 12
    assert sorted(prepared["strata"].values()) == [4] * 6
    assert prepared["double_review"] == 4
    assert all(d["review_status"] == "pending" and d["entities"] == [] for d in gold["documents"])
    groups = [
        {d["group_id"] for d in gold["documents"] if d["split"] == split}
        for split in ["development", "heldout"]
    ]
    assert not groups[0] & groups[1]
    with pytest.raises(ValueError, match="unreviewed"):
        evaluate(settings, prepared["gold_path"])


def test_evaluation_reports_detection_normalization_and_relation_separately(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"])
    changed = next(d for d in gold["documents"] if d["split"] == "heldout")
    changed["entities"][0]["normalized_id"] = "HGNC:other-fixture"
    changed["relations"][0]["predicate"] = "associated_with"
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    result = evaluate(settings, prepared["gold_path"])
    holdout = result["splits"]["heldout"]
    assert holdout["detection"]["f1"] == 1.0
    assert holdout["normalization"]["tp"] == 11 and holdout["normalization"]["fp"] == 1
    assert holdout["relation"]["tp"] == 11 and holdout["relation"]["fn"] == 1
    assert holdout["coverage"]["predicted_gene_documents"] == 12
    assert holdout["coverage"]["paper_prediction_coverage"] == 1.0
    assert sum(topic["documents"] for topic in holdout["by_topic"].values()) == 12
    assert holdout["normalization"]["precision_ci95"]["denominator"] == 12
    assert "within-paper dependence" in result["ci_note"]
    assert Path(result["output_path"]).is_file()


def test_frozen_prediction_changes_and_bad_gold_offsets_are_rejected(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"])
    gold["documents"][0]["entities"][0]["start"] = 0
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    with pytest.raises(ValueError, match="source span"):
        evaluate(settings, prepared["gold_path"])
    Path(prepared["predictions_path"]).write_text("{}")
    with pytest.raises(ValueError, match="predictions changed"):
        evaluate(settings, prepared["gold_path"])


def test_default_cohort_is_200_document_groups_with_40_double_reviews(tmp_path):
    settings = populated(tmp_path, count=201)
    with connect(settings) as con:
        con.execute("UPDATE papers SET doi='10.fixture/0' WHERE paper_id=201")
    prepared = prepare_gold(settings)
    assert prepared["documents"] == 200 and prepared["double_review"] == 40
    assert prepared["development"] == prepared["heldout"] == 100
    with pytest.raises(ValueError, match="found 200"):
        prepare_gold(settings, n=202)


def test_joint_source_and_gene_presence_strata_are_explicit_sampling_signals(tmp_path):
    settings = populated(tmp_path)
    with connect(settings) as con:
        con.execute("UPDATE sources SET kind='fulltext' WHERE paper_id BETWEEN 7 AND 12 OR paper_id>=19")
        con.execute("DELETE FROM mentions WHERE paper_id>=13")
    prepared = prepare_gold(settings, n=24)
    gold = json.loads(Path(prepared["gold_path"]).read_text(encoding="utf-8"))
    assert len(prepared["sampling_strata"]) == 24
    assert set(prepared["sampling_strata"].values()) == {1}
    assert {d["source_level"] for d in gold["documents"]} == {"pubmed", "fulltext"}
    assert {d["gene_presence_stratum"] for d in gold["documents"]} == {
        "predicted_present",
        "predicted_absent",
    }
    assert all(d["review_status"] == "pending" and not d["entities"] for d in gold["documents"])
    assert "not a reviewed gold label" in gold["sampling_method"]
    review(prepared["gold_path"], prepared["predictions_path"])
    result = evaluate(settings, prepared["gold_path"])
    coverage = result["splits"]["heldout"]["coverage"]
    assert coverage["predicted_gene_documents"] == 6
    assert coverage["predicted_gene_document_coverage"] == 0.5


def test_wilson_intervals_keep_zero_denominators_undefined_and_report_binomial_scope():
    empty = _metrics(set(), set())
    assert empty["precision"] is empty["recall"] is empty["f1"] is None
    assert empty["precision_ci95"] is empty["recall_ci95"] is None
    missing = _metrics({"gold"}, set())
    assert missing["precision"] is missing["precision_ci95"] is None
    assert missing["recall"] == 0.0
    assert missing["recall_ci95"]["upper"] == pytest.approx(0.7934506856)
    false_positive = _metrics(set(), {"prediction"})
    assert false_positive["recall"] is false_positive["recall_ci95"] is None
    assert false_positive["precision_ci95"]["denominator"] == 1
    mixed = _metrics({"a", "b"}, {"a", "x"})
    assert mixed["precision_ci95"]["lower"] == pytest.approx(0.0945312057)
    assert mixed["precision_ci95"]["upper"] == pytest.approx(0.9054687943)
    assert mixed["ci_method"] == "Wilson 95% binomial approximation"


@pytest.mark.parametrize(("change", "remaining"), [("rules", 23), ("query_version", 23), ("disabled", 20)])
def test_preparation_excludes_stale_rules_and_inactive_topic_membership(tmp_path, change, remaining):
    settings = populated(tmp_path)
    with connect(settings) as con:
        if change == "rules":
            con.execute("UPDATE extractions SET result_json=? WHERE paper_id=1", (dumps({"rules_version": "old-rules"}),))
        elif change == "query_version":
            con.execute("UPDATE paper_topics SET query_version='outdated-query' WHERE paper_id=1")
        else:
            con.execute("UPDATE topics SET enabled=0 WHERE topic_id=(SELECT topic_id FROM paper_topics WHERE paper_id=1)")
    with pytest.raises(ValueError, match=f"found {remaining}"):
        prepare_gold(settings, n=24)


@pytest.mark.parametrize("field", ["split", "group_id", "primary_topic", "topics", "source_level", "source_id",
                                   "extraction_id", "engine_version", "dictionary_version", "rules_version",
                                   "double_review_required", "seed"])
def test_gold_cannot_change_frozen_partition_or_provenance(tmp_path, field):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"])
    document = gold["documents"][0]
    if field == "seed":
        gold["seed"] += 1
    elif field == "split":
        document[field] = "heldout" if document[field] == "development" else "development"
    elif field == "double_review_required":
        document[field] = not document[field]
    elif field == "topics":
        document[field] = ["invented-topic"]
    else:
        document[field] = "changed-fixture-value"
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    with pytest.raises(ValueError, match="Immutable cohort metadata"):
        evaluate(settings, prepared["gold_path"])


def test_editing_prediction_file_and_gold_hash_cannot_replace_recorded_predictions(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"])
    prediction_path = Path(prepared["predictions_path"])
    frozen = json.loads(prediction_path.read_text(encoding="utf-8"))
    frozen["documents"][0]["relations"][0]["predicate"] = "altered-fixture"
    changed = dumps(frozen)
    prediction_path.write_text(changed, encoding="utf-8")
    gold["prediction_sha256"] = digest(changed)
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    with pytest.raises(ValueError, match="recorded cohort hash"):
        evaluate(settings, prepared["gold_path"])


def test_historical_frozen_evaluation_does_not_use_live_latest_rules_or_paper_source(tmp_path, monkeypatch):
    from nutri_literature import extraction

    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    review(prepared["gold_path"], prepared["predictions_path"])
    with connect(settings) as con:
        con.execute("UPDATE papers SET current_source_id='later-source'")
        con.execute("UPDATE extractions SET active=0")
        con.execute("UPDATE topics SET enabled=0")
    monkeypatch.setattr(extraction, "RULES_VERSION", "later-rule-version")
    result = evaluate(settings, prepared["gold_path"])
    assert result["rules_versions"] == [RULES_VERSION]
    assert result["splits"]["heldout"]["detection"]["f1"] == 1.0
    assert result["cohort_id"] == prepared["cohort_id"]


def test_frozen_inputs_include_exact_sections_and_extractor_metadata_without_annotations(tmp_path):
    settings = populated(tmp_path)
    sections = [{"start": 0, "end": 22, "label": "Results"}]
    with connect(settings) as con:
        con.execute("UPDATE sources SET sections=? WHERE paper_id=1", (dumps(sections),))
        con.execute("UPDATE papers SET publication_types=?,mesh=? WHERE paper_id=1",
                    (dumps(["Randomized Controlled Trial"]), dumps(["Blood Pressure"])))
    prepared = prepare_gold(settings, n=24)
    frozen = json.loads(Path(prepared["inputs_path"]).read_text(encoding="utf-8"))
    document = next(document for document in frozen["documents"] if document["pmid"] == "12345678")
    assert document["text"] == "Vitamin D inhibits VDR."
    assert document["sections"] == sections
    assert document["metadata"]["publication_types"] == ["Randomized Controlled Trial"]
    assert document["metadata"]["mesh"] == ["Blood Pressure"]
    assert document["metadata"]["title"] == document["title"]
    assert set(document["topic_versions"]) == set(document["topics"])
    assert all("entities" not in d and "relations" not in d and "reviewers" not in d for d in frozen["documents"])


def test_new_rule_run_reuses_same_100_100_inputs_without_opening_gold_or_baseline(tmp_path, monkeypatch):
    from nutri_literature import extraction

    settings = populated(tmp_path, count=200)
    prepared = prepare_gold(settings)
    original_gold = json.loads(Path(prepared["gold_path"]).read_text(encoding="utf-8"))
    input_documents = json.loads(Path(prepared["inputs_path"]).read_text(encoding="utf-8"))["documents"]
    # The rerun must work while annotation and original prediction files are inaccessible.
    Path(prepared["gold_path"]).rename(Path(prepared["gold_path"]).with_name("sealed-gold.json"))
    Path(prepared["predictions_path"]).rename(Path(prepared["predictions_path"]).with_name("sealed-baseline.json"))
    with connect(settings) as con:
        con.execute("UPDATE papers SET title='Changed live review',publication_types='[\"Review\"]'")
        con.execute("UPDATE sources SET sections='[]',text_content='Changed live source'")
    original_extract = extraction.extract
    calls = []

    def final_rules(text, sections, metadata, dictionaries):
        calls.append((text, sections, metadata))
        return original_extract(text, sections, metadata, dictionaries)

    monkeypatch.setattr(extraction, "RULES_VERSION", "final-fixture-rules")
    monkeypatch.setattr(extraction, "extract", final_rules)
    run = predict_gold(settings, prepared["inputs_path"])
    predictions = json.loads(Path(run["prediction_run"]).read_text(encoding="utf-8"))["documents"]
    assert run["splits"] == {"development": 100, "heldout": 100}
    assert run["rules_versions"] == ["final-fixture-rules"]
    assert {(d["pmid"], d["group_id"], d["split"], d["source_hash"]) for d in predictions} == {
        (d["pmid"], d["group_id"], d["split"], d["source_hash"]) for d in original_gold["documents"]
    }
    assert calls == [(d["text"], d["sections"], d["metadata"]) for d in input_documents]
    assert run["status"] == "predictions_only_no_gold_labels_read"


def test_development_evaluation_ignores_pending_or_malformed_heldout_labels(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"], split="development")
    for document in gold["documents"]:
        if document["split"] == "heldout":
            document["entities"] = "sealed labels are not available to development scoring"
            document["relations"] = [{"invalid": "this must not be inspected"}]
            document["reviewers"] = [None]
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    baseline = evaluate(settings, prepared["gold_path"], split="development")
    assert baseline["documents"] == 12 and baseline["cohort_documents"] == 24
    assert set(baseline["splits"]) == {"development"}
    run = predict_gold(settings, prepared["inputs_path"])
    candidate = evaluate(settings, prepared["gold_path"], prediction_run=run["prediction_run"], split="development")
    assert set(candidate["splits"]) == {"development"}
    assert candidate["prediction_run_id"] == run["run_id"]
    assert candidate["prediction_scope"] == "independent_rule_run"
    with pytest.raises(ValueError, match="unreviewed"):
        evaluate(settings, prepared["gold_path"], prediction_run=run["prediction_run"])


def test_final_rule_run_can_score_same_heldout_gold_without_changing_original_baseline(tmp_path, monkeypatch):
    from nutri_literature import extraction

    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    review(prepared["gold_path"], prepared["predictions_path"])
    gold_bytes = Path(prepared["gold_path"]).read_bytes()
    baseline_bytes = Path(prepared["predictions_path"]).read_bytes()
    monkeypatch.setattr(extraction, "RULES_VERSION", "final-fixture-rules")
    run = predict_gold(settings, prepared["inputs_path"])
    final = evaluate(settings, prepared["gold_path"], prediction_run=run["prediction_run"], split="heldout")
    assert final["documents"] == 12 and set(final["splits"]) == {"heldout"}
    assert final["rules_versions"] == ["final-fixture-rules"]
    assert final["prediction_sha256"] == run["prediction_sha256"]
    assert final["baseline_prediction_sha256"] == digest(baseline_bytes)
    assert Path(prepared["gold_path"]).read_bytes() == gold_bytes
    assert Path(prepared["predictions_path"]).read_bytes() == baseline_bytes
    baseline = evaluate(settings, prepared["gold_path"])
    assert baseline["rules_versions"] == [RULES_VERSION]
    assert baseline["prediction_scope"] == "preparation_time_baseline"


@pytest.mark.parametrize("field", ["text", "sections", "metadata", "split"])
def test_changed_inputs_are_refused_even_when_editable_gold_hash_is_replaced(tmp_path, field):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"])
    inputs_path = Path(prepared["inputs_path"])
    frozen = json.loads(inputs_path.read_text(encoding="utf-8"))
    document = frozen["documents"][0]
    if field == "metadata":
        document[field]["publication_types"] = ["Review"]
    elif field == "sections":
        document[field] = [{"start": 0, "end": 22, "label": "References"}]
    elif field == "split":
        document[field] = "heldout" if document[field] == "development" else "development"
    else:
        document[field] = "Altered source"
    changed = dumps(frozen)
    inputs_path.write_text(changed, encoding="utf-8")
    with pytest.raises(ValueError, match="Frozen inputs.*recorded cohort hash"):
        predict_gold(settings, inputs_path)
    gold["input_sha256"] = digest(changed)
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    with pytest.raises(ValueError, match="input reference.*recorded cohort hash"):
        evaluate(settings, prepared["gold_path"])


def test_changed_prediction_run_and_run_from_other_cohort_are_refused(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    review(prepared["gold_path"], prepared["predictions_path"])
    run = predict_gold(settings, prepared["inputs_path"])
    run_path = Path(run["prediction_run"])
    frozen = json.loads(run_path.read_text(encoding="utf-8"))
    frozen["documents"][0]["rules_version"] = "altered-fixture-rules"
    run_path.write_text(dumps(frozen), encoding="utf-8")
    with pytest.raises(ValueError, match="Prediction run.*recorded hash"):
        evaluate(settings, prepared["gold_path"], prediction_run=run_path)
    other_cohort = prepare_gold(settings, n=24, seed=20261002)
    other_run = predict_gold(settings, other_cohort["inputs_path"])
    with pytest.raises(ValueError, match="different frozen input cohort"):
        evaluate(settings, prepared["gold_path"], prediction_run=other_run["prediction_run"])


def test_legacy_baseline_without_frozen_inputs_remains_evaluable(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    gold = review(prepared["gold_path"], prepared["predictions_path"])
    run = predict_gold(settings, prepared["inputs_path"])
    # Simulate the older preparation format, including its historical audit receipt.
    del gold["input_file"], gold["input_sha256"]
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    Path(prepared["inputs_path"]).unlink()
    with connect(settings) as con:
        record = con.execute("SELECT event_id,detail FROM audit WHERE kind='gold_prepared'").fetchone()
        detail = json.loads(record["detail"])
        del detail["input_sha256"]
        con.execute("UPDATE audit SET detail=? WHERE event_id=?", (dumps(detail), record["event_id"]))
    assert evaluate(settings, prepared["gold_path"])["documents"] == 24
    with pytest.raises(ValueError, match="no pinned extractor inputs"):
        evaluate(settings, prepared["gold_path"], prediction_run=run["prediction_run"])


@pytest.mark.parametrize("command", ["predict-gold", "rerun-predictions"])
def test_prediction_cli_dispatch_reads_input_path(command, tmp_path, monkeypatch, capsys):
    from nutri_literature import cli, evaluation

    settings = populated(tmp_path)
    input_path = tmp_path / "frozen_inputs.json"
    calls = []

    def fake_predict(settings, path):
        calls.append((settings.home, path))
        return {"status": "fixture-prediction-dispatch"}

    monkeypatch.setattr(evaluation, "predict_gold", fake_predict)
    monkeypatch.setattr("sys.argv", ["nutri-literature", "--home", str(settings.home), command, str(input_path)])
    cli.main()
    assert calls == [(settings.home, input_path)]
    assert json.loads(capsys.readouterr().out)["status"] == "fixture-prediction-dispatch"


def test_evaluation_cli_passes_prediction_run_and_selected_split(tmp_path, monkeypatch, capsys):
    from nutri_literature import cli, evaluation

    settings = populated(tmp_path)
    gold_path, run_path = tmp_path / "gold.json", tmp_path / "prediction-run.json"
    calls = []

    def fake_evaluate(settings, path, prediction_run=None, split="both"):
        calls.append((settings.home, path, prediction_run, split))
        return {"status": "fixture-evaluation-dispatch"}

    monkeypatch.setattr(evaluation, "evaluate", fake_evaluate)
    monkeypatch.setattr("sys.argv", ["nutri-literature", "--home", str(settings.home), "evaluate", str(gold_path),
                                     "--prediction-run", str(run_path), "--split", "development"])
    cli.main()
    assert calls == [(settings.home, gold_path, run_path, "development")]
    assert json.loads(capsys.readouterr().out)["status"] == "fixture-evaluation-dispatch"


def test_evaluation_records_pending_acceptance_without_fabricating_owner_choice(tmp_path):
    settings = populated(tmp_path)
    prepared = prepare_gold(settings, n=24)
    review(prepared["gold_path"], prepared["predictions_path"])
    result = evaluate(settings, prepared["gold_path"])
    assert result["acceptance"]["status"] == "owner_decision_pending"
    assert "criterion_met" not in result["acceptance"]
    assert result["cohort_split_documents"] == {"development": 12, "heldout": 12}


def test_explicit_acceptance_contract_is_hashed_and_preserves_gold_and_baseline(tmp_path):
    settings = populated(tmp_path, count=200)
    prepared = prepare_gold(settings)
    review(prepared["gold_path"], prepared["predictions_path"])
    before = {name: Path(prepared[name]).read_bytes() for name in ("gold_path", "predictions_path")}
    contract = {
        "schema_version": "literature-acceptance-v1", "declared_by": "synthetic-fixture-owner",
        "decision_reference": "Synthetic software test; no original contract claimed",
        "source_kind": "repository_plan", "endpoint": "detection", "entity_kind": None,
        "interpretation": "point_estimate", "precision_target": .90, "recall_target": .70,
    }
    path = tmp_path / "fixture-contract.json"
    path.write_text(dumps(contract), encoding="utf-8")
    result = evaluate(settings, prepared["gold_path"], split="heldout", acceptance_contract=path)
    assert result["acceptance"]["contract_sha256"] == digest(path.read_bytes())
    assert result["acceptance"]["status"] == "declared_threshold_met"
    assert result["cohort_double_review_documents"] == 40
    assert {name: Path(prepared[name]).read_bytes() for name in before} == before
    # Complete heldout annotations alone cannot satisfy the 200/40 annotation plan.
    gold = json.loads(Path(prepared["gold_path"]).read_text(encoding="utf-8"))
    for document in gold["documents"]:
        if document["split"] == "development":
            document["review_status"] = "pending"
            document["reviewers"] = []
            document["adjudicated"] = False
    Path(prepared["gold_path"]).write_text(dumps(gold), encoding="utf-8")
    partial = evaluate(settings, prepared["gold_path"], split="heldout", acceptance_contract=path)
    assert partial["splits"]["heldout"]["detection"]["f1"] == 1.0
    assert partial["cohort_reviewed_documents"] == 100
    assert partial["cohort_double_review_documents"] == 40
    assert partial["cohort_completed_double_review_documents"] < 40
    assert partial["acceptance"]["status"] == "complete_cohort_manual_gold_pending"
    assert "criterion_met" not in partial["acceptance"]


@pytest.mark.parametrize("reviewers,adjudicated", [
    ("fake two reviewers", True), (["Same Reviewer", " same reviewer "], True),
    (["one", "two"], "false"),
])
def test_review_metadata_cannot_fake_distinct_adjudicated_reviewers(reviewers, adjudicated):
    from nutri_literature.evaluation import _validate_review

    with pytest.raises(ValueError):
        _validate_review({"review_status": "reviewed", "reviewers": reviewers,
                          "double_review_required": True, "adjudicated": adjudicated})
