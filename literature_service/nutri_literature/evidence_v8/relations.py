"""Document-local mention grouping and span-aligned semantic supervision."""
from __future__ import annotations

from .common import POLICY, TYPES, atomic, canonical, digest, exact, home, metric, read
from .data import documents, source_only


def native_truth(document):
    kinds = {}
    for e in document.get("entities", []):
        for identifier in e.get("native_ids", []):
            kinds.setdefault(str(identifier), set()).add(e["kind"].replace("component", "chemical"))
    result = set()
    for r in document.get("relations", []):
        a, b, typ = str(r["entity1"]), str(r["entity2"]), r["predicate"]
        if typ not in TYPES:
            continue
        if "chemical" in kinds.get(a, set()) and "gene" in kinds.get(b, set()):
            result.add((a, b, typ))
        elif "gene" in kinds.get(a, set()) and "chemical" in kinds.get(b, set()):
            result.add((b, a, typ))
    return result


def span_ids(document, node):
    return {str(identifier) for m in node["mentions"] for e in document.get("entities", [])
            if e["kind"].replace("component", "chemical") == node["kind"]
            and (m["start"], m["end"]) == (e["start"], e["end"])
            for identifier in e.get("native_ids", [])}


def semantic_labels(document, chemical, gene):
    # The selected automatic IDs have no role in assigning semantic truth.
    a, b = span_ids(document, chemical), span_ids(document, gene)
    return sorted({t for x, y, t in native_truth(document) if x in a and y in b})


def build_rows(split, decisions):
    from ..repair_v6_gpu_r2.text import clusters, pair_views
    docs = documents("relations-train" if split == "train" else "development-sources")
    result = []
    for d in docs:
        if split != "train":
            source_only(d)
        p = read(home()/"v8-prepared"/split/("paper-"+str(d["pmid"])+".json"))
        nodes = clusters(p["mentions"], d["text"])
        chemicals = [n for n in nodes if n["kind"] == "chemical"]
        genes = [n for n in nodes if n["kind"] == "gene"]
        for c in chemicals:
            for g in genes:
                views = pair_views(d["text"], c, g, max_views=8)
                if not views:
                    continue
                def assigned(m):
                    return decisions.get((str(d["pmid"]), m["kind"], m["start"], m["end"]), {}).get("selected_id", m.get("previous_id"))
                lead = min(views, key=lambda v: (not all(assigned(m) for m in v["endpoints"]),
                    abs(v["endpoints"][0]["start"]-v["endpoints"][1]["start"]), v["endpoints"][0]["start"]))
                row = {"pmid": str(d["pmid"]), "pair_key": digest(canonical([d["pmid"], c["key"], g["key"]]))[:24],
                       "views": views, "ids": [assigned(m) for m in lead["endpoints"]],
                       "endpoints": lead["endpoints"], "source_sha256": digest(d["text"]),
                       "source_kind": "document_relation_hypothesis", "direct_evidence_confirmed": False}
                if split == "train":
                    if d.get("relation_annotation_complete") is False:
                        continue
                    row["semantic_types"] = semantic_labels(d, c, g)
                    row["exists"] = bool(row["semantic_types"])
                    a, b = span_ids(d, c), span_ids(d, g)
                    roles = set()
                    for r in d.get("relations", []):
                        if not ((str(r["entity1"]) in a and str(r["entity2"]) in b)
                                or (str(r["entity2"]) in a and str(r["entity1"]) in b)):
                            continue
                        if "subject" in r:
                            subject = str(r["subject"])
                            if subject in a:
                                roles.add(0)
                            elif subject in b:
                                roles.add(1)
                            elif subject in ("None", "null", "", "undirected"):
                                roles.add(2)
                    row["direction_target"] = next(iter(roles)) if len(roles) == 1 else -100
                    row["strict_types"] = sorted(t for x, y, t in native_truth(d) if [x, y] == row["ids"])
                    # No synthetic sentence evidence labels from document labels.
                    row["evidence_targets"] = None
                result.append(row)
    return result


def accepted(rows, scores, threshold):
    result = set()
    for r, score in zip(rows, scores):
        if not all(r["ids"]) or score["exists"] < threshold:
            continue
        for typ, value in zip(TYPES, score["types"]):
            if value >= threshold:
                result.add((r["pmid"], r["ids"][0], r["ids"][1], typ))
    return result


def expected(documents):
    return {(str(d["pmid"]), a, b, t) for d in documents for a, b, t in native_truth(d)}


def select_policy(rows, scores, gold_documents, baseline_recall=0.):
    gold = expected(gold_documents)
    if not gold:
        raise ValueError("관계 검증 정답 0건")
    floor = max(65/154, baseline_recall)
    options = []
    for threshold in POLICY["thresholds"]:
        m = metric(gold, accepted(rows, scores, threshold))
        options.append({"threshold": threshold, **m, "recall_floor": floor})
    passed = [v for v in options if v["precision"] >= .85 and v["recall"] >= floor]
    keeping_recall = [v for v in options if v["recall"] >= floor]
    if passed:
        selected = max(passed, key=lambda v: (v["recall"], v["f1"], v["threshold"]))
        reason = "training_goal_passed"
    elif keeping_recall:
        selected = max(keeping_recall, key=lambda v: (v["precision"], v["recall"], v["f1"]))
        reason = "precision_goal_not_met"
    else:
        selected = max(options, key=lambda v: (v["recall"], v["precision"], v["f1"]))
        reason = "recall_goal_not_met"
    return {**selected, "passed": bool(passed), "selection_reason": reason, "full_gold_denominator": len(gold)}


def paper_prediction(document, rows, scores, threshold):
    source_only(document)
    relations = []
    for row, score in zip(rows, scores):
        if row["pmid"] != str(document["pmid"]) or not all(row["ids"]) or score["exists"] < threshold:
            continue
        # Confidence is a ranking score, never represented as nutrition accuracy.
        evidence = []
        seen = set()
        for view in row["views"]:
            for span in view["segments"]:
                key = (span["start"], span["end"])
                if key not in seen:
                    if not exact(document["text"], span):
                        raise ValueError("원문 인용 위치 불일치")
                    evidence.append(span)
                    seen.add(key)
        if not all(exact(document["text"], span) for span in row["endpoints"]):
            raise ValueError("관계 참여 개체 위치 불일치")
        direction = max(range(3), key=lambda i: score["direction"][i])
        subject = row["ids"][direction] if direction in (0, 1) and score["direction"][direction] >= .5 else None
        for typ, value in zip(TYPES, score["types"]):
            if value >= threshold:
                relations.append({"entity1": row["ids"][0], "entity2": row["ids"][1], "predicate": typ,
                    "subject": subject, "endpoints": row["endpoints"], "evidence": evidence,
                    "priority": min(score["exists"], value), "source_kind": "document_relation_hypothesis",
                    "direct_evidence_confirmed": False, "human_reviewed": False})
    return {"pmid": str(document["pmid"]), "engine": "nutriomics-evidence-v8",
            "source_sha256": digest(document["text"]), "relations": relations,
            "nutrition_accuracy": "unmeasured"}
