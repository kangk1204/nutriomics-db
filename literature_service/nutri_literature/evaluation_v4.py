"""Source-only prediction followed by full-denominator scoring and failure stages.

Previously sealed corpora are never opened for tuning. Development predictions
are immutable and resumable. A final evaluation consumes an exclusive attempt
marker before inference, including failed attempts.
"""
from collections import Counter
import json
from pathlib import Path
import time

from . import benchmarks as scoring
from .db import digest, dumps, now, uid

STAGES = ("source_alignment", "entity_span", "species", "identity", "relation_candidate",
          "role", "context", "evaluation_id_conversion")


def project_source_event(event,labels,mapping,oracle=False):
    """BioRED labels source relationships, not verified observed dietary effects.

Background source facts are scored for that annotated endpoint and remain
ineligible for our observed-result map. Plans and uncertain/negated claims are
not converted into positive relationships. Observation eligibility is reported
separately and never inferred from this benchmark.
    """
    if event.get("source_relation_candidate") is False or event.get("assertion_state")=="planned" or "gene_identity_unresolved" in event.get("triage_reasons",[]):
        return None
    return scoring._project_event({**event,"status":"candidate"},labels,mapping,oracle)


def failure_stages(document, prediction, mapping, oracle=False):
    rows = []
    detected = prediction["entities"]
    for relation in document.get("relations", []):
        sides = [[e for e in document["entities"] if relation[side] in e.get("native_ids", [])]
                 for side in ("entity1", "entity2")]
        if not all(sides) or {sides[0][0]["kind"], sides[1][0]["kind"]} != {"gene", "component"}:
            continue
        matching = [[p for p in detected if any(p["kind"] == e["kind"] and p["start"] == e["start"] and p["end"] == e["end"]
                                              for e in side)] for side in sides]
        reasons = []
        stage = None
        if prediction.get("source_hash") != digest(document["text"]):
            stage = "source_alignment"
        elif not all(matching):
            stage = "entity_span"
        else:
            genes = [m for side in matching for m in side if m["kind"] == "gene"]
            if not oracle and not any(m.get("payload", {}).get("taxon_id") for m in genes):
                stage = "species"
            elif not all(any(m.get("normalized_id") for m in side) for side in matching):
                stage = "identity"
            else:
                event_matches = [event for event in prediction.get("events", []) if all(any(
                    role["start"] == mention["start"] and role["end"] == mention["end"] for mention in side
                ) for role, side in zip((event["controller"], event["theme"]), matching)) or all(any(
                    role["start"] == mention["start"] and role["end"] == mention["end"] for mention in side
                ) for role, side in zip((event["theme"], event["controller"]), matching))]
                if not event_matches:
                    pairs = prediction.get("candidate_pairs", [])
                    available = any(all(any(p["start"] == m["start"] and p["end"] == m["end"] for m in side)
                                        for p, side in zip(pair, matching)) or all(any(
                                            p["start"] == m["start"] and p["end"] == m["end"] for m in side)
                                            for p, side in zip(reversed(pair), matching)) for pair in pairs)
                    stage = "role" if available else "relation_candidate"
                elif any({p["entity1"],p["entity2"]}=={relation["entity1"],relation["entity2"]}
                         and p["predicate"]==relation["predicate"] and (not relation.get("subject") or p.get("subject")==relation["subject"])
                         for p in prediction.get("relations",[])):
                    stage=None
                elif all(e.get("assertion_state")=="planned" or e.get("negation") or e.get("speculation") for e in event_matches):
                    stage = "context"
                    reasons = sorted({r for e in event_matches for r in e.get("triage_reasons", [])})
                elif not any({p["entity1"], p["entity2"]} == {relation["entity1"], relation["entity2"]}
                             for p in prediction.get("relations", [])):
                    stage = "evaluation_id_conversion"
                elif not any({p["entity1"], p["entity2"]} == {relation["entity1"], relation["entity2"]}
                             and p["predicate"] == relation["predicate"]
                             and (not relation.get("subject") or p.get("subject") == relation["subject"])
                             for p in prediction.get("relations", [])):
                    stage = "role"
                    reasons = ["relation_type_or_direction_mismatch"]
        rows.append({"relation": relation, "first_failure": stage, "additional_reasons": reasons})
    return rows


def strict_gene_counts(document, prediction, mapping, oracle=False):
    expected = {(e["start"], e["end"], identifier) for e in document["entities"] if e["kind"] == "gene"
                for identifier in e.get("native_ids", [])}
    observed = set()
    for e in prediction["entities"]:
        if e["kind"] != "gene":
            continue
        values = {e["normalized_id"]} if oracle and e.get("normalized_id") else mapping.get(("gene", e.get("normalized_id")), set())
        if len(values) == 1:
            observed.add((e["start"], e["end"], next(iter(values))))
        else:
            # Unresolved detected candidates count as errors, not removed output.
            observed.add((e["start"], e["end"], "UNRESOLVED"))
    return scoring._metrics(expected, observed)


def _mapping(settings, dictionaries, values, lexicon_path):
    mapping, manifest = scoring._crosswalk(settings, dictionaries)
    for record in values["_gene_index"].records:
        mapping[("gene", record["id"])] = {record["native_id"]}
    raw = json.loads(Path(lexicon_path).read_bytes())
    for record in raw["records"]:
        mapping[("component", record["id"])] = {record["native_id"]}
    return mapping, manifest


def development(settings, corpus, lexicon_path, provider="biomedbert", mode="end_to_end", limit=200, profile="local"):
    from .operations_v4 import components, identity
    from .dictionaries import load_dictionaries
    from .chemical_v3 import load_snapshot
    from .registry_v4 import extract_engine
    index = json.loads((settings.home / "public_benchmarks" / "v2-measurement-index.json").read_bytes())
    prepared = index["steps"]["prepare:development:" + corpus + ":abstract"]
    inputs, receipt, input_sha = scoring._load_inputs(settings, prepared["inputs_path"])
    if inputs["split"] != "development":
        raise ValueError("V4 development runner cannot access protected tests")
    documents = sorted(inputs["documents"], key=lambda d: digest("v3-development-20261003:" + d["pmid"]))[:limit]
    values = components(settings, provider)
    dictionaries = load_dictionaries(settings, "heuristic-v2")
    artifact = identity(settings, values, dictionaries, profile)
    experiment = {"artifact": artifact, "input_sha256": input_sha, "corpus": corpus, "mode": mode,
                  "profile": profile, "selected_pmids": [d["pmid"] for d in documents],
                  "chemical_sha256": digest(Path(lexicon_path).read_bytes())}
    directory = settings.home / "experiments" / "v4" / digest(dumps(experiment))
    directory.mkdir(parents=True, exist_ok=True)
    identity_path = directory / "identity.json"
    if not identity_path.exists():
        scoring._write_immutable(identity_path, experiment)
    if json.loads(identity_path.read_bytes()) != experiment:
        raise ValueError("Experiment identity changed")
    completed = directory / "report.json"
    if completed.exists():
        report = json.loads(completed.read_bytes())
        if not scoring.verify_receipt(settings, "public_v4_development_experiment", report):
            raise ValueError("Completed experiment receipt changed")
        return report
    oracle = {}
    if mode == "gold_entities":
        raw = Path(prepared["inputs_path"]).with_name("oracle_entities.json").read_bytes()
        if digest(raw) != receipt["oracle_entities_sha256"]:
            raise ValueError("Entity oracle changed")
        oracle = {d["pmid"]: d for d in json.loads(raw)["documents"]}
        if any(set(d) != {"pmid", "source_hash", "entities"} for d in oracle.values()):
            raise ValueError("Oracle contains nonentity labels")
    mapping, _ = _mapping(settings, dictionaries, values, lexicon_path)
    lexicon = load_snapshot(lexicon_path)
    predictions, elapsed = {}, time.monotonic()
    for document in documents:
        path = directory / ("paper-" + document["pmid"] + ".json")
        if path.exists():
            saved = json.loads(path.read_bytes())
            if saved["artifact_identity"] != artifact["artifact_identity"] or saved["source_hash"] != document["source_hash"]:
                raise ValueError("Resume artifact/source changed")
        else:
            metadata = {**values, "_chemical_lexicon": lexicon, "profile": profile,
                        "_artifact_identity": artifact["artifact_identity"]}
            if mode == "gold_entities":
                metadata["_gold_entities"] = [e for e in oracle[document["pmid"]]["entities"] if e["kind"] in {"gene", "component"}]
            if profile == "pubtator_assisted":
                from .pubtator_v4 import fetch
                external = fetch(document["pmid"], document["text"], directory / "external")
                if external["status"] != "aligned":
                    raise ValueError("PubTator input profile unavailable: " + external["status"])
                metadata["_external_entities"] = external["entities"]
                metadata["_external_response_sha256"] = external["raw_sha256"]
            result = extract_engine(document["text"], document["sections"], dictionaries, metadata, "heuristic-v4")
            labels = {"entities": oracle.get(document["pmid"], {}).get("entities", [])}
            relations = [projected for event in result["events"]
                         if (projected := project_source_event(event, labels, mapping, mode == "gold_entities"))]
            observed_relations=[p for p in relations if p["source_event"]["status"]=="candidate"
                                and p["source_event"].get("assertion_state")=="observed"]
            saved = {"pmid": document["pmid"], "group_id": document["group_id"], "source_hash": document["source_hash"],
                     "artifact_identity": artifact["artifact_identity"], "entities": result["mentions"],
                     "events": result["events"], "relations": relations, "observed_relations":observed_relations,"scopes": result["benchmark_scopes"],
                     "annotated_spans": result["annotated_spans"], "summary": result["summary"],
                     "diagnostics": result["diagnostics"], "candidate_pairs": result["candidate_pairs"], "manifest": result["manifest"]}
            scoring._write_immutable(path, saved)
        predictions[document["pmid"]] = saved
        if len(predictions) % 10 == 0:
            print(dumps({"corpus": corpus, "provider": provider, "mode": mode, "processed": len(predictions)}), flush=True)
    # No relational/scopes/span labels are read until predictions are persisted.
    gold, _, gold_sha = scoring._load_gold(settings, prepared["gold_path"])
    tasks, _ = scoring._paper_annotation_tasks(gold, receipt, prepared["gold_path"])
    labels = {d["pmid"]: d for d in gold["documents"]}
    rows, failures = [], []
    for doc in documents:
        pmid = doc["pmid"]
        counts = scoring._paper_counts(labels[pmid], predictions[pmid], mapping, mode, annotated_tasks=tasks[pmid])
        if "gene_detection" in tasks[pmid]:
            counts["gene_strict_id"] = strict_gene_counts(labels[pmid], predictions[pmid], mapping, mode == "gold_entities")
        rows.append({"pmid": pmid, **counts})
        failures.extend({"pmid": pmid, **r} for r in failure_stages(labels[pmid], predictions[pmid], mapping, mode == "gold_entities"))
    endpoints = sorted({k for row in rows for k, v in row.items() if isinstance(v, dict) and {"tp", "fp", "fn"} <= set(v)})
    metrics = {endpoint: scoring._aggregate(rows, endpoint) for endpoint in endpoints}
    report = {"schema_version": "literature-v4-development-v1", "id": uid(), "at": now(), "identity": experiment,
              "corpus": corpus, "mode": mode, "provider": provider, "profile": profile, "split": "development",
              "papers": len(documents), "available_papers": len(inputs["documents"]), "metrics": metrics,
              "per_paper": rows, "failures": failures, "failure_counts": dict(Counter(r["first_failure"] or "matched" for r in failures)),
              "gene_identity_reasons": dict(Counter(m.get("payload", {}).get("identity_reason") or "resolved"
                                                  for p in predictions.values() for m in p["entities"] if m["kind"] == "gene")),
              "gold_sha256": gold_sha, "elapsed_seconds": time.monotonic() - elapsed,
              "provenance_valid": all(r["provenance_valid"] == r["provenance_total"] for r in rows),
              "promotion_eligible": False, "nutrition_accuracy": "unmeasured", "review_time_saved": "unmeasured",
              "pretraining_overlap": "unknown", "output_path": str(completed)}
    report["relation_evaluation_scope"]="source_statement_relation; observed-result eligibility is separate"
    report["observed_relation_candidates"]=sum(len(p["observed_relations"]) for p in predictions.values())
    baseline_key = "predict:development:" + corpus + ":abstract:" + mode + ":heuristic-v2"
    if baseline_key in index["steps"]:
        baseline = json.loads(Path(index["steps"][baseline_key]["prediction_run"]).read_bytes())
        if not scoring.verify_receipt(settings, "public_benchmark_prediction", baseline):
            raise ValueError("Baseline receipt changed")
        original_mapping, _ = scoring._crosswalk(settings, dictionaries)
        old = {d["pmid"]: d for d in baseline["documents"]}
        baseline_rows = {doc["pmid"]: scoring._paper_counts(labels[doc["pmid"]], old[doc["pmid"]], original_mapping,
                         mode, annotated_tasks=tasks[doc["pmid"]]) for doc in documents}
        candidate_rows = {r["pmid"]: r for r in rows}
        report["paired_bootstrap"] = {endpoint: scoring.paired_bootstrap(sorted(predictions), baseline_rows, candidate_rows,
                                         endpoint, 2000, 20261009) for endpoint in endpoints if endpoint != "gene_strict_id"}
    scoring._write_immutable(completed, report)
    scoring._receipt(settings, "public_v4_development_experiment", report["id"], report)
    return report


def reports(settings, profile=None):
    items = []
    for path in sorted((settings.home / "experiments" / "v4").glob("*/report.json")):
        value = json.loads(path.read_bytes())
        if profile and value.get("profile") != profile:
            continue
        if scoring.verify_receipt(settings, "public_v4_development_experiment", value):
            items.append({k: v for k, v in value.items() if k not in {"per_paper", "failures", "identity"}})
    return {"engine": "heuristic-v4", "items": items, "nutrition_accuracy": "unmeasured", "default_promotion": "gated"}


def select_provider(reports_):
    eligible = [r for r in reports_ if r["mode"] == "end_to_end" and r["profile"] == "local"
                and (r["metrics"].get("gene_detection", {}).get("precision") or 0) >= .9]
    if not eligible:
        return {"selected": None, "status": "comparison_beta", "reason": "gene_precision_below_90_percent"}
    winner = min(eligible, key=lambda r: (-(r["metrics"].get("gene_strict_id", {}).get("f1") or 0), r["elapsed_seconds"]))
    return {"selected": winner["provider"], "status": "development_selected", "report_id": winner["id"]}


def freeze(settings, audit_path, output_path):
    """Pin an audited fresh test cohort; never read its annotation labels here."""
    from .operations_v4 import components, identity
    from .dictionaries import load_dictionaries
    audit=json.loads(Path(audit_path).read_bytes())
    if audit.get("schema")!="v4-holdout-audit" or audit.get("completed") is not True:
        raise ValueError("Completed PMID/PMCID/text and train/dev/test overlap audit is required")
    allowed={"nlmgene","nlmchem","pubmedrct","evidenceinference"}
    specs=audit.get("specs",[])
    if {s["corpus"] for s in specs}!=allowed:
        raise ValueError("Freeze requires all four declared fresh final corpora")
    values=components(settings)
    artifact=identity(settings,values,load_dictionaries(settings,"heuristic-v2"))
    for spec in specs:
        for field in ("inputs","gold"):
            if digest(Path(spec[field+"_path"]).read_bytes())!=spec[field+"_sha256"]:
                raise ValueError("Audited test file changed")
        if spec.get("official_split")!="test" or spec.get("training_overlap_articles",0):
            raise ValueError("Known supervised training overlap must be removed from training before freezing")
    receipt={"schema":"v4-final-freeze","frozen_at":now(),"artifact":artifact,"audit_sha256":digest(Path(audit_path).read_bytes()),
             "specs":specs,"attempt_policy":"one attempt per corpus, including failed attempts",
             "pretraining_overlap":"unknown; independent pretraining separation is not established"}
    scoring._write_immutable(Path(output_path),receipt)
    return {"freeze_path":str(output_path),"sha256":digest(Path(output_path).read_bytes())}


def final_evaluate(settings, spec_path, freeze_path):
    """Normalized fresh-corpus adapter with one-attempt and provenance checks."""
    from .operations_v4 import components, identity
    from .dictionaries import load_dictionaries
    from .registry_v4 import extract_engine
    from .chemical_v3 import load_snapshot
    spec=json.loads(Path(spec_path).read_bytes())
    frozen=json.loads(Path(freeze_path).read_bytes())
    values=components(settings)
    dictionaries=load_dictionaries(settings,"heuristic-v2")
    artifact=identity(settings,values,dictionaries)
    if frozen.get("schema")!="v4-final-freeze" or artifact!=frozen["artifact"] or spec not in frozen["specs"]:
        raise ValueError("Final test is not in this unchanged frozen experiment")
    if spec["corpus"] not in {"nlmgene","nlmchem","pubmedrct","evidenceinference"}:
        raise ValueError("Previously sealed corpora cannot be used for v4 tuning")
    directory=Path(freeze_path).parent/spec["corpus"]
    directory.mkdir(parents=True,exist_ok=True)
    with (directory/"attempt.json").open("x",encoding="utf-8") as stream:
        stream.write(dumps({"started_at":now(),"freeze_sha256":digest(Path(freeze_path).read_bytes())}))
    raw=Path(spec["inputs_path"]).read_bytes()
    if digest(raw)!=spec["inputs_sha256"]:
        raise ValueError("Final source-only inputs changed")
    inputs=json.loads(raw)
    if inputs["split"]!="test" or any(set(d)&{"entities","relations","scopes","annotated_spans","labels"} for d in inputs["documents"]):
        raise ValueError("Final prediction inputs must contain no labels")
    lexicon=load_snapshot(spec["chemical_lexicon_path"])
    mapping,_=_mapping(settings,dictionaries,values,spec["chemical_lexicon_path"])
    predictions={}
    for document in inputs["documents"]:
        if digest(document["text"])!=document["source_hash"]:
            raise ValueError("Final source hash changed")
        if spec["corpus"]=="pubmedrct":
            sentences=document["rhetorical_sentences"]
            result={"mentions":[],"events":[],"candidate_pairs":[],"benchmark_scopes":[],"annotated_spans":[],
                    "sentence_roles":values["_clinical_models"].predict(sentences)}
        else:
            result=extract_engine(document["text"],document["sections"],dictionaries,
                                  {**values,"_chemical_lexicon":lexicon,"_artifact_identity":artifact["artifact_identity"]},"heuristic-v4")
        predicted={"pmid":document["pmid"],"source_hash":document["source_hash"],"entities":result["mentions"],
                   "events":result["events"],"candidate_pairs":result["candidate_pairs"],"scopes":result["benchmark_scopes"],
                   "annotated_spans":result["annotated_spans"],"sentence_roles":result["sentence_roles"],
                   "relations":[p for e in result["events"] if e["status"]=="candidate" and (p:=scoring._project_event(e,{"entities":[]},mapping))]}
        scoring._write_immutable(directory/("paper-"+document["pmid"]+".json"),predicted)
        predictions[document["pmid"]]=predicted
    # Opening gold is deliberately below the persisted full prediction loop.
    raw=Path(spec["gold_path"]).read_bytes()
    if digest(raw)!=spec["gold_sha256"]:
        raise ValueError("Frozen final labels changed")
    gold=json.loads(raw)
    if {d["pmid"] for d in gold["documents"]}!=set(predictions):
        raise ValueError("Final prediction/gold document sets differ")
    rows=[]
    role_expected,role_observed=[],[]
    for document in gold["documents"]:
        predicted=predictions[document["pmid"]]
        counts=scoring._paper_counts(document,predicted,mapping,"end_to_end",annotated_tasks=spec.get("annotation_tasks",[]))
        if spec["corpus"]=="nlmgene":
            counts["gene_strict_id"]=strict_gene_counts(document,predicted,mapping)
        if spec["corpus"]=="nlmchem":
            expected={(m["start"],m["end"]) for m in document["entities"] if m["kind"]=="component"}
            observed={(m["start"],m["end"]) for m in predicted["entities"] if m["kind"] in {"component","nutrient"}}
            counts["chemical_detection"]=scoring._metrics(expected,observed)
        if spec["corpus"]=="pubmedrct":
            roles={(s["start"],s["end"]):s["rhetorical_role"] for s in predicted["sentence_roles"]}
            for s in document["rhetorical_labels"]:
                role_expected.append(s["label"].casefold());role_observed.append(roles.get((s["start"],s["end"]),"unaligned"))
        if spec["corpus"]=="evidenceinference":
            # Comparative evidence is a separate endpoint, never a BioRED claim.
            expected={(p["prompt_id"],q["start"],q["end"]) for p in document["comparisons"] for q in p["evidence"]}
            observed=set()
            for prompt in document["comparisons"]:
                for event in predicted["events"]:
                    controller=event["controller"]["text"].casefold()
                    target=event["theme"]["text"].casefold()
                    if controller==prompt["intervention"].casefold() and target==prompt["outcome"].casefold() and event["conditions"].get("comparator"):
                        observed.update((prompt["prompt_id"],q["start"],q["end"]) for q in event["evidence_quotes"])
            counts["comparison_evidence_exact"]=scoring._metrics(expected,observed)
        rows.append({"pmid":document["pmid"],**counts})
    endpoints={k for r in rows for k,v in r.items() if isinstance(v,dict) and {"tp","fp","fn"}<=set(v)}
    report={"schema":"v4-final-report","at":now(),"corpus":spec["corpus"],"split":"test","papers":len(rows),
            "metrics":{e:scoring._aggregate(rows,e) for e in endpoints},"per_paper":rows,
            "intervals":{e:scoring._paper_intervals(rows,e,2000,20261009) for e in endpoints},
            "freeze_sha256":digest(Path(freeze_path).read_bytes()),"pretraining_overlap":"unknown",
            "nutrition_accuracy":"unmeasured","review_time_saved":"unmeasured","promotion_eligible":False}
    if role_expected:
        from sklearn.metrics import accuracy_score,f1_score
        report["rhetorical_role"]={"accuracy":accuracy_score(role_expected,role_observed),
                                   "macro_f1":f1_score(role_expected,role_observed,average="macro"),"sentences":len(role_expected)}
    scoring._write_immutable(directory/"report.json",report)
    return report
