"""One-shot protected evaluation; test annotations open only after predictions."""

import json
from pathlib import Path
import zipfile

from . import benchmarks as scoring
from .db import digest, dumps, now, uid


def evaluate(settings, spec_path, freeze):
    from .operations_v6 import components, identity
    from .dictionaries import load_dictionaries
    from .evaluation_v4 import _mapping
    from .evaluation_v6 import _metrics_for, project_hypotheses
    from .registry_v6 import extract_engine
    from .chemical_v6 import load_snapshot
    from .corpora_v5 import FINAL_MEMBER, RELEASE_SHA, source_only
    from .public_corpora import parse_pubtator

    spec = json.loads(Path(spec_path).read_bytes())
    if spec.get("corpus") in {"nlmgene", "nlmchem", "pubmedrct", "evidenceinference"}:
        from .holdout_v6 import evaluate as normalized_evaluate

        return normalized_evaluate(settings, spec, freeze)
    if set(spec) != {"audit", "inputs", "release", "lexicon", "corpus"} or spec["corpus"] != "bc8":
        raise ValueError("V6 relation final spec requires explicit BC8 source paths")
    audit = json.loads(Path(spec["audit"]).read_bytes())
    if (
        digest(Path(spec["audit"]).read_bytes()) != freeze["audit_sha256"]
        or audit["bc8_annotation_labels_read"]
    ):
        raise ValueError("Final audit differs or labels already consumed")
    values = components(settings, freeze["artifact"]["candidate_provider"]["gene_provider"])
    dictionaries = load_dictionaries(settings, "heuristic-v2")
    artifact = identity(settings, values, dictionaries)
    if artifact != freeze["artifact"]:
        raise ValueError("Artifact differs from freeze")
    if (
        digest(Path(spec["lexicon"]).read_bytes())
        != values["_chemical_lexicon"]["manifest"]["snapshot_sha256"]
    ):
        raise ValueError("Final dictionary differs from frozen artifact")
    inputs = json.loads(Path(spec["inputs"]).read_bytes())
    if inputs["split"] != "test" or len(inputs["documents"]) != 400:
        raise ValueError("Complete BC8 400-paper source cohort required")
    archive = Path(spec["release"])
    if digest(archive.read_bytes()) != RELEASE_SHA:
        raise ValueError("Final raw release changed")
    with zipfile.ZipFile(archive) as release:
        raw = release.read(FINAL_MEMBER).decode()
    expected = source_only(raw)
    protected_hash = inputs.get("final_member_sha256") or audit.get("final_member_sha256")
    protected_records = [
        r
        for r in audit.get("protected_sources", [])
        if r.get("filename", r["path"].replace("\\", "/").split("/")[-1]) == Path(spec["inputs"]).name
        and r.get("sha256") == digest(Path(spec["inputs"]).read_bytes())
    ]
    if expected != inputs["documents"] or digest(raw) != protected_hash or not protected_records:
        raise ValueError("Final source cohort differs")
    directory = settings.home / "experiments/v6/final"
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "attempt.json"
    if marker.exists():
        raise ValueError("Protected final attempt already consumed, including partial or failed attempts")
    # Exclusive creation before predicting; no retry with new tuning after exposure.
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(
            dumps(
                {
                    "artifact_identity": artifact["artifact_identity"],
                    "spec_sha256": digest(Path(spec_path).read_bytes()),
                    "started_at": now(),
                }
            )
        )
    lexicon = load_snapshot(spec["lexicon"])
    mapping, _ = _mapping(settings, dictionaries, values, spec["lexicon"])
    predicted = {}
    for doc in inputs["documents"]:
        result = extract_engine(
            doc["text"],
            doc["sections"],
            dictionaries,
            {**values, "_chemical_lexicon": lexicon, "_artifact_identity": artifact["artifact_identity"]},
            "heuristic-v6",
        )
        relations, verified = project_hypotheses(result, mapping)
        saved = {
            "pmid": doc["pmid"],
            "entities": result["mentions"],
            "relations": relations,
            "verified_relations": verified,
            "artifact_identity": artifact["artifact_identity"],
            "source_hash": doc["source_hash"],
        }
        scoring._write_immutable(directory / ("paper-" + doc["pmid"] + ".json"), saved)
        predicted[doc["pmid"]] = saved
    # This is the first test-label parse in this workflow.
    gold = parse_pubtator(raw)
    papers = [
        {"pmid": d["pmid"], **_metrics_for(d, predicted[d["pmid"]], mapping, False, "bioredirect")}
        for d in gold
    ]
    # Entity/ID oracle is a separate endpoint, after automatic predictions are sealed.
    oracle_papers = []
    for doc in gold:
        result = extract_engine(
            doc["text"],
            doc["sections"],
            dictionaries,
            {
                **values,
                "_chemical_lexicon": lexicon,
                "_artifact_identity": artifact["artifact_identity"],
                "_gold_entities": doc["entities"],
            },
            "heuristic-v6",
        )
        relations, verified = project_hypotheses(result, mapping, True)
        saved = {
            "pmid": doc["pmid"],
            "entities": result["mentions"],
            "relations": relations,
            "verified_relations": verified,
            "artifact_identity": artifact["artifact_identity"],
            "source_hash": doc["source_hash"],
            "entity_oracle": True,
        }
        scoring._write_immutable(directory / ("paper-oracle-" + doc["pmid"] + ".json"), saved)
        oracle_papers.append({"pmid": doc["pmid"], **_metrics_for(doc, saved, mapping, True, "bioredirect")})
    endpoints = (
        "gene",
        "gene_strict_ids",
        "relation",
        "relation_direction",
        "relation_direction_all",
        "verified_relation",
    )
    report = {
        "id": uid(),
        "at": now(),
        "schema": "v6-independent-bc8-final",
        "artifact": artifact,
        "papers": 400,
        "metrics": {e: scoring._aggregate(papers, e) for e in endpoints},
        "per_paper": papers,
        "oracle_metrics": {e: scoring._aggregate(oracle_papers, e) for e in endpoints},
        "oracle_per_paper": oracle_papers,
        "intervals": {e: scoring._paper_intervals(papers, e, 2000, 20261010) for e in endpoints},
        "nutrition_accuracy": "unmeasured",
        "pretraining_overlap": "unknown",
        "one_shot": True,
    }
    scoring._write_immutable(directory / "report.json", report)
    scoring._receipt(settings, "v6_final_evaluation", report["id"], report)
    return report
