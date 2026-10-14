"""Versioned curation import; source-checked claims retain pending expert review.

Existing result contrasts are discovered by their full analysis key and linked,
not copied. New study metadata never masquerades as downloaded assay data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select

from nutrigenomics.bridge_models import (
    BridgeSource,
    EvidenceAssertion,
    Exposure,
    InterventionArm,
    StudyContrast,
)
from nutrigenomics.db import create_schema, get_session_factory
from nutrigenomics.models import (
    AnalysisRun,
    Component,
    GeoSeries,
    KnowledgeLink,
    ResearchStudy,
    StudyEffect,
)
from nutrigenomics.settings import DATABASE_URL, PROJECT_ROOT


def upsert(session, model, key, **values):
    row = session.get(model, key)
    if row is None:
        row = model(id=key, **values)
        session.add(row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    return row


def import_bridge(path: Path, database_url=DATABASE_URL) -> dict:
    raw = path.read_bytes()
    doc = json.loads(raw)
    version = doc["version"]
    digest = hashlib.sha256(raw).hexdigest()
    create_schema(database_url)
    counts = {"studies": 0, "exposures": 0, "contrasts": 0, "assertions": 0, "legacy_links": 0}
    with get_session_factory(database_url)() as session:
        for source in session.scalars(select(BridgeSource).where(BridgeSource.version == version)):
            if source.sha256 != digest:
                raise ValueError("Published curation version has different content; use a new version")
        for spec in doc["studies"]:
            study_id = spec["id"]
            existing_study = session.get(ResearchStudy, study_id)
            previous = (existing_study.metadata_json or {}) if existing_study else {}
            designs=dict(previous.get("series_design") or {})
            for accession in spec["series"]:
                previous_design=designs.get(accession,{})
                designs[accession]={**previous_design,"tissue":spec["tissue"],
                    "sample_count_basis":"Source-checked public omics subset; parent clinical cohort is separate",
                    **({"reported_people":spec["n_people"]} if spec.get("n_people") is not None else {}),
                    **({"biological_samples":spec["n_samples"]} if spec.get("n_samples") is not None else {})}
            upsert(session, ResearchStudy, study_id, title=spec["title"], description=spec["description"],
                   pubmed_id=spec.get("pubmed_id"), independent_study=True,
                   metadata_json={**previous, "study_design": spec["design"], "limitations": spec["limitations"],
                                  "primary_sources": spec["sources"], "bridge_version":version,
                                  "expert_review_status":"pending", "series":spec["series"],"series_design":designs,
                                  "clinical_cohort_people":13 if study_id=="N15" else previous.get("clinical_cohort_people")})
            session.flush()
            for accession in spec["series"]:
                series = session.get(GeoSeries, accession)
                if series is None:
                    series = GeoSeries(accession=accession, study_id=study_id, title=spec["title"],
                                       organism="Homo sapiens", pubmed_id=spec.get("pubmed_id"),
                                       assay_type=spec.get("assay_type"), sample_count=spec.get("n_samples"),
                                       metadata_verified=False, assay_data_downloaded=False,
                                       metadata_json={"source":spec["sources"][0],"registry_only":True})
                    session.add(series)
                else:
                    m = dict(series.metadata_json or {})
                    m.update(bridge_version=version, curation_limitations=spec["limitations"])
                    series.metadata_json = m
            source_id = f"human-source:{study_id}:{version}"
            upsert(session, BridgeSource, source_id, url=spec["sources"][0]["url"], version=version,
                   sha256=digest, source_location=spec["source_location"],
                   license="Original sources retain their terms; curator-authored paraphrases and identifiers only.",
                   export_allowed=True, metadata_json={"curation_manifest":str(path),
                   "source_snapshot_status":"source_urls_and_manifest_archived", "sources":spec["sources"],
                   "curator":"Codex source-backed curation", "expert_review_status":"pending"})
            session.flush()
            exposure_ids = []
            for x in spec["exposures"]:
                eid = f"{study_id}:{x['key']}:{version}"
                exposure_ids.append(eid)
                upsert(session, Exposure, eid, study_id=study_id, label=x["label"], kind=x["kind"],
                       formulation=x["formulation"], amount=x.get("amount"), unit=x.get("unit"),
                       dose_basis=x["dose_basis"], duration_days=x.get("duration_days"),
                       component_keys=x.get("component_keys",[]), food_terms=x.get("food_terms",[]),
                       source_id=source_id, metadata_json={**x.get("metadata",{}),
                       "component_causality_identified":False, "expert_review_status":"pending"})
                session.flush()
                # Versioned exposure assertions make missing dose metadata inspectable.
                upsert(session, EvidenceAssertion, f"exposure:{eid}", study_id=study_id,
                       exposure_id=eid, contrast_id=None, source_id=source_id, layer="exposure",
                       assertion=x["label"], review_status="source_checked", organism="Homo sapiens",
                       inference="descriptive", limitations=spec["limitations"],
                       metadata_json={"expert_review_status":"pending", "clinical_data_status":"not_linked"})
                counts["exposures"] += 1
            session.flush()
            arm_ids=[]
            for arm in spec.get("arms",[]):
                aid=f"arm:{study_id}:{arm['key']}:{version}"
                arm_ids.append(aid)
                exposure_keys={x["key"]:eid for x,eid in zip(spec["exposures"],exposure_ids,strict=True)}
                upsert(session,InterventionArm,aid,study_id=study_id,label=arm["label"],
                    allocation=spec["allocation"],exposure_ids=[exposure_keys[k] for k in arm["exposure_keys"]],
                    n_people=arm.get("n_people"),metadata_json={"role":arm["role"],
                    "n_people_scope":arm["n_people_scope"],"allocation_scope":spec.get("allocation_scope","intervention"),
                    "arm_key":arm["key"]})
            session.flush()
            for accession in spec["series"]:
                # No effect row scan: distinct versions and contrasts are bounded by study.
                run_versions = [r.analysis_version for r in session.scalars(select(AnalysisRun).where(
                    AnalysisRun.series_accession == accession, AnalysisRun.status == "completed"))]
                for av in run_versions:
                    contrasts = session.execute(select(StudyEffect.contrast, StudyEffect.tissue,
                        StudyEffect.n_people, StudyEffect.n_samples).where(StudyEffect.series_accession==accession,
                        StudyEffect.analysis_version==av).distinct()).all()
                    unique = {}
                    for name,tissue,np,ns in contrasts:
                        unique.setdefault(name,(tissue,np,ns))
                    for name,(tissue,np,ns) in unique.items():
                        selected = []
                        for x,eid in zip(spec["exposures"],exposure_ids,strict=True):
                            pattern=x.get("contrast_pattern")
                            if not pattern or re.search(pattern,name):
                                selected.append(eid)
                        if not selected:
                            continue
                        match=re.search(r"(?:_|^)([246])h(?:_|$)",name)
                        time_h=float(match.group(1)) if match else (6.0 if name.startswith("six_hr") else 3.0 if name.startswith("three_hr") else None)
                        cid=f"{accession}:{av}:{name}"
                        if len(cid)>220:
                            cid=accession+":"+hashlib.sha256(cid.encode()).hexdigest()
                        comparison_arms=[aid for arm,aid in zip(spec.get("arms",[]),arm_ids,strict=True)
                            if re.search(r"(?:^|_|_vs_?)"+re.escape(arm["key"])+r"(?:_|$)",name,re.IGNORECASE)]
                        arm_status="identified_from_contrast" if comparison_arms else "study_context_only"
                        upsert(session, StudyContrast, cid, study_id=study_id, series_accession=accession,
                            contrast=name,analysis_version=av,comparison=name,design=spec["analysis_design"],
                            tissue=spec["tissue"],response_time_h=time_h,exposure_ids=selected,
                            arm_ids=comparison_arms or arm_ids,n_people=np,n_samples=ns,
                            analysis_status="reanalyzed", metadata_json={"legacy_tissue_label":tissue,
                            "person_independent_unit":True,"component_causality_identified":False,
                            "arm_selection_status":arm_status,
                            "n_samples_scope":"source_reported_assay_count; consult analysis metadata for fitted/contrast scope",
                            "comparison_baseline_h":3 if name=="six_hr_vs_three_hr" else 0 if "vs_fast" in name else None})
                        session.flush()
                        for eid in selected:
                            endpoints=[ep for ep in spec.get("clinical_endpoints",[]) if not ep.get("contrast_pattern") or re.search(ep["contrast_pattern"],name)]
                            aid="observed:"+hashlib.sha256((cid+eid).encode()).hexdigest()
                            upsert(session,EvidenceAssertion,aid,study_id=study_id,exposure_id=eid,
                                contrast_id=cid,source_id=source_id,layer="molecular_measurement",
                                assertion="공개 중재 시료의 해당 대비에서 관찰된 분자 결과; 개별 성분의 독립 효과가 아님.",
                                review_status="source_checked",organism="Homo sapiens",inference="association",
                                limitations=spec["limitations"],metadata_json={"expert_review_status":"pending",
                                "clinical_data_status":"published_aggregate_only" if endpoints else "not_linked",
                                "clinical_endpoints":endpoints,
                                "hypothesis_family":"see analysis provenance"})
                            counts["assertions"]+=1
                        counts["contrasts"]+=1
            # Only a small exposure-level legacy projection; no gene/CpG effect duplication.
            for x,eid in zip(spec["exposures"],exposure_ids,strict=True):
                for key in x.get("component_keys",[]):
                    if session.scalar(select(Component.id).where(Component.component_key==key)) is None:
                        continue
                    source_ref="bridge:"+hashlib.sha256(eid.encode()).hexdigest()[:32]
                    link=session.scalar(select(KnowledgeLink).where(KnowledgeLink.source=="human_intervention_bridge",
                        KnowledgeLink.source_ref==source_ref,KnowledgeLink.subject_id==key))
                    if link is None:
                        session.add(KnowledgeLink(source="human_intervention_bridge",source_ref=source_ref,
                            subject_type="component",subject_id=key,relation="shared_component_of_intervention",
                            target_type="exposure",target_id=eid,evidence_kind="related_human_intervention",
                            organism="Homo sapiens",tissue=spec["tissue"],dose=x["dose_basis"],
                            duration=str(x.get("duration_days")) if x.get("duration_days") is not None else "acute or unreported",
                            pubmed_id=spec.get("pubmed_id"),metadata_json={"study_id":study_id,
                            "source_url":spec["sources"][0]["url"],"bridge_version":version,
                            "component_specific_causality":False,"individual_prediction":False}))
                        counts["legacy_links"]+=1
            counts["studies"]+=1
        session.commit()
    return {**counts,"version":version,"curation_manifest_sha256":digest,
            "expert_review_status":"pending","individual_prediction":False}


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--manifest",type=Path,default=PROJECT_ROOT/"config"/"human_exposures.v1.json")
    parser.add_argument("--database-url",default=DATABASE_URL)
    args=parser.parse_args()
    print(json.dumps(import_bridge(args.manifest,args.database_url),ensure_ascii=False,indent=2))
