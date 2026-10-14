"""Machine-verifiable composition coverage; biological relevance awaits expert review."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select

from nutrigenomics.bridge import meal_evidence
from nutrigenomics.composition import analyze_meal
from nutrigenomics.db import get_session_factory
from nutrigenomics.models import CompositionMeasurement, Food


def report(database_url:str, selection:Path)->dict:
    doc=json.loads(selection.read_text(encoding="utf-8"))
    rows=[]
    with get_session_factory(database_url)() as session:
        for spec in doc["foods"]:
            food=session.scalar(select(Food).where(
                Food.metadata_json["provider_food_code"].as_string()==spec["food_code"],
                Food.metadata_json["kfind_dataset_id"].as_string()==spec["dataset_id"],
                Food.metadata_json["selection_version"].as_string()==doc["version"]))
            if food is None:
                # Some importers preserve dataset prefix in the original code.
                food=session.scalar(select(Food).where(Food.source_code.endswith(spec["food_code"])))
            if food is None:
                rows.append({"name":spec["name"],"source_code":spec["food_code"],"status":"missing"})
                continue
            calculation=analyze_meal(session,[{"food_id":food.id,"amount_g":100}])
            evidence=meal_evidence(session,calculation)
            records=list(session.scalars(select(CompositionMeasurement).where(CompositionMeasurement.food_id==food.id)))
            active=[r for r in records if r.release==food.active_release]
            missing_trace=[r.id for r in active if not r.release or not r.source_ref or not (r.metadata_json or {}).get("source_record_sha256")]
            rows.append({"id":food.id,"name":food.name,"source_code":food.source_code,
                "release":food.active_release,"preparation_state":food.preparation_state,
                "status":"traceable" if not missing_trace else "incomplete_trace",
                "composition_count":len(active),"calculated_component_count":len(calculation["components"]),
                "basis_values":sorted({r.basis for r in active}),
                "missing_trace_ids":missing_trace,
                "related_observed_assertions":len([r for r in evidence["observed_evidence"] if r["result_status"]=="reanalyzed"]),
                "related_studies":sorted({r["study_id"] for r in evidence["observed_evidence"] if r["result_status"]=="reanalyzed"}),
                "direct_context_match_count":sum(r["applicability"]["classification"]=="direct_context_match" for r in evidence["observed_evidence"]),
                "evidence_gap_count":len(evidence["evidence_gaps"])})
    n=len(rows)
    absent=sum(r.get("related_observed_assertions",0)==0 for r in rows)
    return {"selection_version":doc["version"],"selection_sha256":hashlib.sha256(selection.read_bytes()).hexdigest(),
        "food_count":n,"traceable_food_count":sum(r["status"]=="traceable" for r in rows),
        "without_related_omics_count":absent,"without_related_omics_fraction":absent/n if n else None,
        "interpretation":"Shared macronutrients retrieve related mixed interventions; coverage is not food-specific causality or personal predictability.",
        "expert_review_status":"pending", "foods":rows}


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--database-url",required=True)
    parser.add_argument("--selection",type=Path,default=Path("config/kfind_food_selection.json"))
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    payload=report(args.database_url,args.selection)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in payload.items() if k!="foods"},ensure_ascii=False))
