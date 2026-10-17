"""Freeze real-data cases before changes; retain every warm and concurrent sample."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import math
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def freeze(database, core, output):
    with sqlite3.connect(Path(database).resolve().as_uri()+"?mode=ro", uri=True) as db:
        meta = {r[0]: json.loads(r[1]) for r in db.execute("SELECT * FROM meta")}
        food50 = [r[0] for r in db.execute("SELECT id FROM node WHERE kind='food' AND id LIKE 'K-FIND:%' ORDER BY id")]
        assert len(food50) == 50
        high = {kind: db.execute("SELECT id FROM node WHERE kind=? ORDER BY degree DESC,id LIMIT 1", (kind,)).fetchone()[0] for kind in ("food", "gene", "chemical")}
        yogurt = "FDC:2259793"
        assert db.execute("SELECT 1 FROM node WHERE id=?", (yogurt,)).fetchone()
        orphan = db.execute("SELECT id,kind FROM node WHERE degree=0 ORDER BY id LIMIT 1").fetchone()
        contrasts = [json.loads(r[0]) for r in db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast'")]
    cases = []
    def add(name, path, params=None, payload=None, target=1):
        cases.append({"id": name, "path": "/api/v1/explorer/"+path, "params": params or {}, "payload": payload, "target_s": target})
    add("food-search", "entities", {"kind":"food", "q":"흰쌀밥"})
    add("gene-degree", "entities", {"kind":"gene", "domain":"lipids", "sort":"degree", "order":"desc"})
    add("region-substring", "entities", {"kind":"regulatory_region", "q":"EH38E"})
    add("empty-search", "entities", {"kind":"food", "q":"__no_such_food_20261010__"})
    for name, seed in [("yogurt",yogurt),("rice",food50[0]),("APOE","NCBIGene:348"),("hypertension","MESH:D006973"),("high-food",high["food"]),("high-gene",high["gene"]),("high-chemical",high["chemical"])]:
        add(name+"-graph", "graph", {"seed_id":seed})
    if orphan:
        add("orphan-graph", "graph", {"seed_id":orphan[0]})
    add("yogurt-max-graph", "graph", {"seed_id":yogurt,"max_nodes":250,"max_edges":750},target=3)
    add("high-gene-max-graph", "graph", {"seed_id":high["gene"],"max_nodes":250,"max_edges":750},target=3)
    add("yogurt-filtered-graph", "graph", {"seed_id":yogurt,"domain":"blood_pressure","tier":"curated"})
    add("yogurt-match", "match", payload={"seed_id":yogurt,"intake_g":150})
    add("yogurt-context-match", "match", payload={"seed_id":yogurt,"intake_g":800,"reference_context":{"tissue":"whole blood","response_time_h":6}})
    add("yogurt-gene-match", "match", payload={"seed_id":yogurt,"intake_g":150,"gene_node_id":"NCBIGene:348"})
    add("APOE-details", "nodes/NCBIGene%3A348")
    add("APOE-evidence", "evidence", {"seed_id":"NCBIGene:348"})
    add("APOE-loci", "loci", {"seed_id":"Ensembl:ENSG00000130203"})
    with sqlite3.connect(Path(core).resolve().as_uri()+"?mode=ro",uri=True) as db:
        for series, target_contrast in [("GSE98645","yogurt_vs_milk_6h_change"),("E-MTAB-12527",None)]:
            candidates = [c for c in contrasts if c.get("series_accession")==series and (target_contrast is None or c.get("contrast")==target_contrast)]
            assert candidates, series
            c=candidates[0]
            types=dict(db.execute("SELECT feature_type,count(*) FROM study_effect WHERE series_accession=? AND contrast=? AND analysis_version=? GROUP BY feature_type",[c[k] for k in ("series_accession","contrast","analysis_version")]))
            native="gene" if "gene" in types else next(iter(types))
            total=types[native]
            params={"series":series,"feature_type":native,"contrast":c["contrast"],"analysis_version":c["analysis_version"],"page_size":25}
            for label,page in [("first",1),("middle",max(1,math.ceil(total/25)//2)),("last",math.ceil(total/25))]:
                add(series+"-"+label,"omics/results",{**params,"page":page})
    result={"frozen_at":datetime.now(UTC).isoformat(),"baseline_release":meta.get("release_id"),"food50":food50,"cases":cases,"high_degree_seeds":high}
    result["case_sha256"]=digest(cases)
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result


def request(base, case, timeout=12):
    url=base+case["path"]+("?"+urllib.parse.urlencode(case["params"]) if case["params"] else "")
    data=json.dumps(case["payload"]).encode() if case.get("payload") is not None else None
    req=urllib.request.Request(url,data=data,headers={"Content-Type":"application/json"} if data else {})
    t=time.perf_counter()
    try:
        with urllib.request.urlopen(req,timeout=timeout) as r:
            body=r.read(); status=r.status; headers=dict(r.headers)
        parsed=json.loads(body)
        return {"seconds":time.perf_counter()-t,"status":status,"bytes":len(body),"body_sha256":digest(parsed),"cache":headers.get("x-explorer-cache"),"server_timing":headers.get("server-timing"),"release_id":parsed.get("release_id")},parsed
    except (TimeoutError, OSError, ValueError, urllib.error.URLError) as exc:
        return {"seconds":time.perf_counter()-t,"error":str(exc),"status":getattr(exc,"code",None)},None


def check_response(sample, data, expected=None):
    """A fast 200 with missing effects or a changed release is not a success."""
    issues = []
    def inspect(value):
        if isinstance(value, dict):
            for field in ("effects_status", "source_effects_status"):
                if field in value and value[field] != "available":
                    issues.append(field + ":" + str(value[field]))
            if value.get("status") in {"source_unavailable", "query_timeout", "analysis_key_mismatch", "source_changed", "query_failed"}:
                issues.append("status:" + value["status"])
            if value.get("result_status") == "source_unavailable":
                issues.append("result_status:source_unavailable")
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    if data is not None:
        inspect(data)
        sample["body_sha256"] = digest(data)
        sample["release_id"] = data.get("release_id")
        if expected is not None and sample["body_sha256"] != expected:
            issues.append("response_changed_during_measurement")
    if issues:
        sample["validation_errors"] = sorted(set(issues))
    return sample


def metrics(samples):
    durations=sorted(s["seconds"] for s in samples)
    return {"n":len(samples),"p50_s":statistics.median(durations),"p95_s":durations[math.ceil(.95*len(durations))-1],"p99_s":durations[math.ceil(.99*len(durations))-1],"max_s":max(durations),"errors":sum(s.get("error") is not None or s.get("status")!=200 or bool(s.get("validation_errors")) for s in samples)}


class ResourceSampler:
    """Server-local /proc observations; no other process or global cache changes."""
    def __init__(self, pid=None):
        self.pid = pid
        self.samples = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            row = {"unix": time.time()}
            for name in ("io", "cpu", "memory"):
                path = Path("/proc/pressure") / name
                if path.is_file():
                    row[name + "_pressure"] = path.read_text()
            if self.pid:
                base = Path("/proc") / str(self.pid)
                for name in ("status", "io", "stat"):
                    try:
                        row["process_" + name] = (base / name).read_text()
                    except OSError:
                        row["process_" + name] = None
            self.samples.append(row)
            self.stop.wait(0.25)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)


def benchmark(base, spec_path, output, runs=100, baseline=False, concurrent_mode=False, source_manifest=None):
    spec=json.loads(Path(spec_path).read_text(encoding="utf-8"));out=Path(output);out.mkdir(parents=True,exist_ok=True)
    if digest(spec["cases"]) != spec["case_sha256"]:
        raise ValueError("Frozen cases changed; do not silently replace the evaluation set")
    results=[]
    expected = {}
    for c in spec["cases"]:
        first,data=request(base,c)
        check_response(first, data)
        expected[c["id"]] = first.get("body_sha256")
        def measured(case):
            sample, parsed = request(base, case)
            return check_response(sample, parsed, expected.get(case["id"]))
        if data is not None:
            (out/(c["id"]+"-response.json")).write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
        samples=[]
        if first.get("error") or first.get("status") != 200 or first.get("validation_errors"):
            samples=[first]
        elif baseline:
            samples=[measured(c) for _ in range(3)]
        else:
            samples=[measured(c) for _ in range(runs)]
        row={"id":c["id"],"target_s":c["target_s"],"first":first,"metrics":metrics(samples),"samples":samples}
        results.append(row)
        (out/"progress.json").write_text(json.dumps({"completed":len(results),"total":len(spec["cases"]),"latest":row["id"],"metrics":row["metrics"]},indent=2),encoding="utf-8")
    concurrent_results={}
    if concurrent_mode:
        jobs=[c for _ in range(runs) for c in spec["cases"]]
        def run(c):
            return c["id"],measured(c)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for name,sample in pool.map(run,jobs):
                concurrent_results.setdefault(name,[]).append(sample)
    report={"verified_at":datetime.now(UTC).isoformat(),"base":base,"case_sha256":spec["case_sha256"],"runs_per_case":3 if baseline else runs,"warmup":"First request excluded; OS caches not dropped", "results":results,
            "all_warm_pass":all(r["metrics"]["errors"]==0 and r["metrics"]["n"]==runs and r["metrics"]["p95_s"]<=r["target_s"] for r in results) if not baseline else None,
            "concurrent":{name:{"metrics":metrics(samples),"samples":samples} for name,samples in concurrent_results.items()}}
    releases = {r["first"].get("release_id") for r in results}
    report["release_id"] = next(iter(releases)) if len(releases) == 1 else None
    if source_manifest:
        with Path(source_manifest).open("rb") as stream:
            report["source_manifest_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    if concurrent_mode:
        report["all_concurrent_pass"]=len(concurrent_results)==len(spec["cases"]) and all(v["metrics"]["n"]==runs and v["metrics"]["errors"]==0 and v["metrics"]["p95_s"]<=(5 if next(c for c in spec["cases"] if c["id"]==name)["target_s"]==3 else 2) for name,v in report["concurrent"].items())
    (out/"performance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with (out/"samples.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=["mode", "case", "sample", "seconds", "status", "bytes", "cache", "body_sha256", "release_id", "server_timing", "error", "validation_errors"])
        writer.writeheader()
        for row in results:
            for mode, samples in (("first", [row["first"]]), ("warm", row["samples"]), ("concurrent4", concurrent_results.get(row["id"], []))):
                for i, sample in enumerate(samples):
                    writer.writerow({"mode":mode, "case":row["id"], "sample":i, **sample})
    return report


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode",choices=["freeze","benchmark"]);p.add_argument("--database");p.add_argument("--core");p.add_argument("--spec");p.add_argument("--base",default="http://127.0.0.1:8020");p.add_argument("--output",required=True);p.add_argument("--runs",type=int,default=100);p.add_argument("--baseline",action="store_true");p.add_argument("--concurrent",action="store_true");p.add_argument("--server-pid",type=int);p.add_argument("--source-manifest")
    a=p.parse_args()
    if a.mode=="freeze":
        r=freeze(a.database,a.core,a.output)
    else:
        with ResourceSampler(a.server_pid) as sampler:
            r=benchmark(a.base,a.spec,a.output,a.runs,a.baseline,a.concurrent,a.source_manifest)
        (Path(a.output)/"resources.json").write_text(json.dumps({"server_pid":a.server_pid,"sampling_interval_s":.25,"available":Path('/proc').is_dir(),"samples":sampler.samples},indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in r.items() if k not in ("results","concurrent","cases","food50")},ensure_ascii=False))
    if a.mode == 'benchmark' and not a.baseline:
        raise SystemExit(0 if r['all_warm_pass'] and (not a.concurrent or r['all_concurrent_pass']) else 1)
