"""Import frozen source sample metadata independently of analysis result matrices."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select

from nutrigenomics.db import get_session_factory
from nutrigenomics.ingest import parse_soft_samples
from nutrigenomics.models import GeoSample, GeoSeries


def direct_plus_samples(session, root: Path, sources: dict) -> int:
    accession = "E-MTAB-12527"
    path = root / "raw" / accession / (accession + ".sdrf.txt")
    source = sources.get(path.name)
    if not source or hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
        raise ValueError("Unverified DIRECT PLUS SDRF")
    with path.open(encoding="utf-8") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader)
        def value(row, name):
            return row[header.index(name)]
        records = {}
        for row in reader:
            if len(row) != len(header):
                raise ValueError("DIRECT PLUS SDRF column mismatch")
            key = value(row, "Source Name")
            records.setdefault(key, []).append(row)
    people = {}
    for rows in records.values():
        row = rows[0]
        people.setdefault(value(row, "Characteristics[individual]"), []).append(
            (value(row, "Factor Value[time]"), value(row, "Factor Value[diet]")))
    if any(len(visits) != 2 or {v[0] for v in visits} != {"0", "18"} or len({v[1] for v in visits}) != 1
           for visits in people.values()):
        raise ValueError("DIRECT PLUS person/visit pairing or arm identity mismatch")
    for key, rows in records.items():
        identities = {(value(r, "Characteristics[individual]"), value(r, "Factor Value[time]"),
                       value(r, "Factor Value[diet]")) for r in rows}
        labels = {value(r, "Label") for r in rows}
        if len(identities) != 1 or len(rows) != 2 or labels != {"Cy3", "Cy5"}:
            raise ValueError("DIRECT PLUS channel/biological-sample identity mismatch")
        person, time_value, arm = next(iter(identities))
        time_unit = rows[0][header.index("Factor Value[time]") + 1]
        sample = session.scalar(select(GeoSample).where(GeoSample.series_accession == accession,
                                                      GeoSample.sample_accession == key))
        if sample is None:
            sample = GeoSample(series_accession=accession, sample_accession=key)
            session.add(sample)
        sample.title = value(rows[0], "Comment[Sample_Name]")
        sample.characteristics = [f"individual: {person}", f"time: {time_value} {time_unit}", f"diet: {arm}"]
        # Preserve duplicate SDRF headers and their positions, including age/time
        # units. DictReader would silently overwrite repeated Unit/Protocol names.
        sample.metadata_json = {"person_id": person, "time_value": time_value, "time_unit": time_unit,
                                "diet_arm": arm, "channel_count": 2, "source_url": source["url"],
                                "source_sha256": source["sha256"], "sdrf_columns": header,
                                "sdrf_records": rows, "expert_review_status": "pending"}
    series = session.get(GeoSeries, accession)
    if series is None:
        raise ValueError("Import DIRECT PLUS study registry before source samples")
    series.sample_count = len(records)
    series.assay_type = "Methylation profiling by array"
    series.platform_ids = sorted({value(r[0], "Array Design REF") for r in records.values()})
    series.organism = "Homo sapiens"
    series.metadata_verified = True
    series.metadata_json = {**(series.metadata_json or {}), "registry_only": False,
                            "sample_metadata_status": "source_verified", "source_sha256": source["sha256"],
                            "source_url": source["url"], "biological_sample_count": len(records),
                            "independent_people": len(people),
                            "channel_count": 2 * len(records)}
    return len(records)


def run(root:Path, database_url:str)->dict:
    doc=json.loads((root/"download_manifest.json").read_text())
    sources={Path(r["path"]).name:r for r in doc["files"] if r.get("status") in {"downloaded","cached_verified"}}
    counts={}
    with get_session_factory(database_url)() as session:
        for accession in ["GSE13466","GSE98645","GSE27385"]:
            path=root/"raw"/accession/(accession+"_family.soft.gz")
            source=sources.get(path.name)
            if not source or hashlib.sha256(path.read_bytes()).hexdigest()!=source["sha256"]:
                raise ValueError("Unverified source sample metadata: "+accession)
            series=session.get(GeoSeries,accession)
            if series is None:
                raise ValueError("Import study registry before source samples: "+accession)
            samples=parse_soft_samples(path)
            if not samples:
                normalized=root/"raw"/accession/"sample_metadata.json"
                normalized_hash=hashlib.sha256(normalized.read_bytes()).hexdigest()
                raw_samples=json.loads(normalized.read_text())
                samples={r["sample_accession"]:{"title":(r.get("title") or [None])[0],
                    "characteristics":r.get("characteristics_ch1",[]),"metadata":r,
                    "channel_characteristics":{"1":r.get("characteristics_ch1",[])}} for r in raw_samples}
                source={"url":"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="+accession,
                    "sha256":normalized_hash,"snapshot_kind":"parsed_GSM_metadata_aggregate",
                    "raw_series_soft_sha256":source["sha256"]}
                path=normalized
            for sid,payload in samples.items():
                row=session.scalar(select(GeoSample).where(GeoSample.series_accession==accession,GeoSample.sample_accession==sid))
                if row is None:
                    row=GeoSample(series_accession=accession,sample_accession=sid)
                    session.add(row)
                row.title=payload.get("title")
                row.characteristics=payload.get("characteristics",[])
                row.metadata_json={**(payload.get("metadata") or {}),"channel_characteristics":payload.get("channel_characteristics",{}),
                    "source_url":source["url"],"source_sha256":source["sha256"],"source_path":str(path),
                    "snapshot_kind":source.get("snapshot_kind","raw_family_soft"),
                    "expert_review_status":"pending"}
            platforms=set()
            for payload in samples.values():
                value=(payload.get("metadata") or {}).get("platform_id",[])
                platforms.update(value if isinstance(value,list) else [value] if value else [])
            if platforms:
                series.platform_ids=sorted(platforms)
            series.assay_type="Expression profiling by high throughput sequencing" if accession=="GSE98645" else "Expression profiling by array"
            series.sample_count=len(samples)
            series.metadata_verified=True
            series.metadata_json={**(series.metadata_json or {}),"registry_only":False,"family_soft_sha256":source["sha256"],
                "family_soft_source_url":source["url"],"sample_metadata_status":"source_verified", "source_sample_count":len(samples)}
            counts[accession]=len(samples)
        if (root / "raw/E-MTAB-12527/E-MTAB-12527.sdrf.txt").is_file():
            counts["E-MTAB-12527"] = direct_plus_samples(session, root, sources)
        session.commit()
    return counts


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,required=True)
    p.add_argument("--database-url",required=True)
    a=p.parse_args()
    print(json.dumps(run(a.root,a.database_url),indent=2))
