"""Independent public-source rebuild, with a versioned 14-outcome analysis DAG.

Run inside a dedicated permanent work directory. No former server or former
effect database is an input. Completion requires executed producer receipts;
process creation and metadata preparation never count as analysis completion.
"""
import argparse
import copy
import json
import os
import subprocess
import re
from datetime import UTC, datetime
from pathlib import Path

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json, writer_lock

SUFFIX = "-rebuild20261017-v2"


def recipe(work, source, suffix=SUFFIX):
    if not re.fullmatch(r"-rebuild[0-9]{8}-v[1-9][0-9]*", suffix):
        raise ValueError("Use a dated, versioned rebuild suffix")
    work, source = Path(work).resolve(), Path(source).resolve()
    policy = json.loads((source / "config/publication_rights_20261014.json").read_text())
    permitted = [r for r in policy["analyses"] if r.get("export_allowed") is True]
    slots = [(r["series_accession"], r["analysis_version"]) for r in permitted]
    outputs = {
        "gse127530-limma-voom-v1": "gse127530-v1",
        "gse56960-rma-limma-v1": "gse56960-v1",
        "gse54325-agilent-limma-v1": "gse54325-v1",
        "gse107205-noob-limma-cpg-v1": "gse107205-v1",
        "gse219217-noob-limma-cpg-v2": "gse219217-v2",
        "gse54643-processed-signal-paired-limma-v1": "gse54643-v1",
        "gse98645-voom-acute-paired-studyfamily-v2": "gse98645-acute-studyfamily-v2",
        "gse98645-voom-chronic-paired-studyfamily-v2": "gse98645-chronic-studyfamily-v2",
        "gse13466-rma-customcdf-limma-paired-v2": "gse13466-raw-v2",
        "gse27385-rma-limma-paired-period-v2": "gse27385-raw-v2",
        "gse56960-rma-limma-dose-change-v3": "gse56960-dose-v3",
        "gse107205-noob-limma-cpg-between-arm-v4": "gse107205-between-arm-v4",
        "gse107205-noob-limma-cpg-between-arm-v4-regions-fixed1000bp-v1": "gse107205-between-arm-v4/regions-fixed1000bp-v1",
        "gse107205-noob-limma-cpg-between-arm-v4-blood-composition-closed-v2": "gse107205-between-arm-v4/blood-composition-closed-v2",
    }
    if set(outputs) != {v for _, v in slots} or len(slots) != 14:
        raise ValueError("The approved 14-outcome scope changed; audit the recipe")
    data, out = work / "data", work / "outputs"
    jobs = []

    def job(key, script, args, folders, requires=(), memory_gib=4):
        jobs.append({"id": key, "script": script, "args": list(map(str, args)),
                         "outputs": folders, "requires": list(requires), "memory_gib": memory_gib})

    def primary(key, script, accession, version, extra=(), memory=4):
        folder = outputs[version]
        args = ["--data-dir", data, "--output-dir", out / folder,
                "--sample-sheet", out / "geo-design-audit" / accession / "sample_design.tsv",
                "--analysis-version", version + suffix, *extra]
        job(key, script, args, [folder], memory_gib=memory)

    primary("rna-counts", "analyze_gse127530.R", "GSE127530", "gse127530-limma-voom-v1")
    primary("acute-expression", "analyze_gse56960.R", "GSE56960", "gse56960-rma-limma-v1")
    primary("flavanol-expression", "analyze_gse54325.R", "GSE54325", "gse54325-agilent-limma-v1")
    primary("flavanol-dnam", "analyze_gse54643_methylation.R", "GSE54643", "gse54643-processed-signal-paired-limma-v1", memory=12)
    primary("long-diet-dnam", "analyze_methylation_geo.R", "GSE107205", "gse107205-noob-limma-cpg-v1", ["--accession", "GSE107205"], 16)
    primary("serial-dnam", "analyze_methylation_geo.R", "GSE219217", "gse219217-noob-limma-cpg-v2", ["--accession", "GSE219217"], 24)
    job("yogurt-raw-models", "analyze_gse98645.R", ["--root", work], ["gse98645-acute-v1", "gse98645-chronic-v1"])
    job("yogurt-study-family", "adjust_gse98645_study_family.R", ["--root", work, "--analysis-version-suffix", suffix],
        ["gse98645-acute-studyfamily-v2", "gse98645-chronic-studyfamily-v2"], ["yogurt-raw-models"])
    for key, accession, script, flag, version in [
        ("fat-crossover", "GSE13466", "analyze_gse13466.R", "--raw-cdf", "gse13466-rma-customcdf-limma-paired-v2"),
        ("fish-oil-crossover", "GSE27385", "analyze_gse27385.R", "--raw-cel", "gse27385-rma-limma-paired-period-v2")]:
        job(key, script, ["--root", work, flag, "true", "--output-dir", out / outputs[version],
                         "--analysis-version", version + suffix], [outputs[version]])
    primary("acute-dose-difference", "analyze_gse56960.R", "GSE56960", "gse56960-rma-limma-dose-change-v3", ["--bridge-contrasts", "true"])
    # The all-person normalization is an explicit upstream preparation, not one
    # of the 14 public outcomes or an invented extra independent cohort.
    job("diet-arm-upstream", "analyze_methylation_geo.R", ["--accession", "GSE107205", "--data-dir", data,
        "--sample-sheet", out / "geo-design-audit/GSE107205/sample_design.tsv", "--output-dir", out / "gse107205-between-arm-v3",
        "--bridge-contrasts", "true", "--analysis-version", "gse107205-noob-limma-cpg-between-arm-v3" + suffix],
        ["gse107205-between-arm-v3"], memory_gib=16)
    base = "gse107205-noob-limma-cpg-between-arm-v4"
    job("diet-arm-qc", "analyze_methylation_qc_sensitivity.R", ["--input-dir", out / "gse107205-between-arm-v3",
        "--output-dir", out / outputs[base], "--analysis-version", base + suffix], [outputs[base]], ["diet-arm-upstream"], 16)
    region = base + "-regions-fixed1000bp-v1"
    job("diet-dmr", "analyze_methylation_regions.R", ["--input-dir", out / outputs[base],
        "--analysis-version", region + suffix], [outputs[region]], ["diet-arm-qc"], 16)
    job("blood-reference", "analyze_blood_composition_sensitivity.R", ["--input-dir", out / outputs[base],
        "--root", work, "--data-dir", data], [outputs[base] + "/blood-composition-v1"], ["diet-arm-qc"], 20)
    closed = base + "-blood-composition-closed-v2"
    job("closed-blood-sensitivity", "analyze_blood_composition_closed_sensitivity.R", ["--input-dir", out / outputs[base],
        "--analysis-version", closed + suffix], [outputs[closed]], ["blood-reference"], 16)
    for entry in jobs:
        entry["source_sha256"] = sha256(source / "scripts" / entry["script"])
    return {"schema_version": 1, "scope": "14 new public analysis outcomes from public originals",
            "former_server_dependency": False, "historical_exact_reproduction_claim": False,
            "scientific_review_status": "pending", "max_threads": 16, "heavy_jobs": 1,
            "runtime_policy": {"R": "4.5.3", "startup_flags": ["--vanilla"], "user_profiles": "disabled"},
            "jobs": jobs, "outcomes": [{"series": s, "historical_version": v, "analysis_version": v + suffix,
                "folder": outputs[v]} for s, v in slots]}


def environment(work, source, project):
    env = os.environ.copy()
    env.update(PYTHONPATH=str(source), NUTRIGENOMICS_DATA_DIR=str(work / "data"),
        NUTRIGENOMICS_DATABASE_URL="sqlite:///" + str(work / "data/db/nutrigenomics.sqlite3"),
        NUTRIGENOMICS_R_LIB=str(project / "toolchain/r-library"),
        R_LIBS_USER=str(project / "toolchain/r-library"), TMPDIR=str(project / "runtime/tmp"),
        OMP_NUM_THREADS="16", OPENBLAS_NUM_THREADS="16", MKL_NUM_THREADS="16", R_THREADS="1")
    env.update(R_PROFILE_USER="/dev/null", R_ENVIRON_USER="/dev/null")
    return env


def execute(work, source, project, suffix=SUFFIX):
    from scripts.freeze_omics_bundle import freeze
    work, source, project = (Path(p).resolve() for p in (work, source, project))
    plan = recipe(work, source, suffix)
    path = work / "analysis-rebuild-plan.json"
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError("Frozen recipe differs; choose a new work version")
    atomic_json(path, plan)
    state_path = work / "analysis-rebuild-status.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"jobs": {}, "executed_public_outcomes": 0}
    env = environment(work, source, project)
    (work / "logs").mkdir(exist_ok=True)
    for job in plan["jobs"]:
        previous = state["jobs"].get(job["id"], {})
        if previous.get("status") == "completed":
            # Re-read the completion gate, including every output hash.
            for folder in job["outputs"]:
                freeze(work / "outputs" / folder)
            continue
        if any(state["jobs"].get(dep, {}).get("status") != "completed" for dep in job["requires"]):
            state["jobs"][job["id"]] = {"status": "dependency_failed", "requires": job["requires"]}
            atomic_json(state_path, state)
            continue
        available = next(int(x.split()[1]) * 1024 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))
        if available < job["memory_gib"] * 1024**3:
            raise MemoryError("Available RAM below job budget: " + job["id"])
        script = source / "scripts" / job["script"]
        if sha256(script) != job["source_sha256"]:
            raise ValueError("Executed source changed")
        record = {"status": "running", "started_at": datetime.now(UTC).isoformat(),
                  "source_sha256": job["source_sha256"], "memory_available_bytes": available,
                  "arguments": job["args"]}
        state["jobs"][job["id"]] = record
        atomic_json(state_path, state)
        log = work / "logs" / (job["id"] + ".log")
        with log.open("ab") as stream:
            proc = subprocess.Popen([str(project / "toolchain/r-nutrigenomics/bin/Rscript"), "--vanilla", str(script), *job["args"]],
                                    cwd=work, env=env, stdout=stream, stderr=subprocess.STDOUT)
            record["pid"] = proc.pid
            atomic_json(state_path, state)
            status = proc.wait()
        record.update(returncode=status, finished_at=datetime.now(UTC).isoformat(),
                      status="completed" if status == 0 else "failed", log=str(log))
        if status == 0:
            try:
                if sha256(script) != job["source_sha256"]:
                    raise ValueError("Source changed during execution")
                for folder in job["outputs"]:
                    freeze(work / "outputs" / folder)
            except (ValueError, OSError, KeyError, TypeError, AssertionError) as exc:
                record.update(status="completion_failed", error=str(exc))
        atomic_json(state_path, state)
    outcomes = []
    for item in plan["outcomes"]:
        gate = work / "outputs" / item["folder"] / "DONE.json"
        complete = gate.exists()
        if complete:
            frozen = freeze(gate.parent)
            complete = frozen["analysis_version"] == item["analysis_version"]
        outcomes.append({**item, "executed_and_frozen": complete})
    state.update(outcomes=outcomes, executed_public_outcomes=sum(r["executed_and_frozen"] for r in outcomes),
                 status="completed" if all(r["executed_and_frozen"] for r in outcomes) else "incomplete",
                 historical_exact_reproduction_claim=False)
    atomic_json(state_path, state)
    return state


def rebuilt_policy(source, work, plan):
    old = json.loads((Path(source) / "config/publication_rights_20261014.json").read_text())
    mapping = {(r["series"], r["historical_version"]): r for r in plan["outcomes"]}
    result = copy.deepcopy(old)
    import hashlib
    identity = json.dumps(plan['outcomes'], sort_keys=True).encode()
    result["version"] = "publication-policy-public-rebuild-" + hashlib.sha256(identity).hexdigest()[:16]
    for row in result["analyses"]:
        key = row["series_accession"], row["analysis_version"]
        if key in mapping:
            row["historical_recipe_reference"] = row["analysis_version"]
            row["analysis_version"] = mapping[key]["analysis_version"]
            row["id"] = "GEO-own-results:" + row["series_accession"] + ":" + row["analysis_version"]
            row["new_source_specific_denials_override"] = True
    output = Path(work) / "publication-rights-rebuild.json"
    atomic_json(output, result)
    return output


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["plan", "execute"])
    p.add_argument("--analysis-suffix", default=SUFFIX)
    for name in ("work", "source", "project"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    if not args.work.resolve().is_relative_to(args.project.resolve()):
        p.error("work must stay inside the project")
    args.work.mkdir(parents=True, exist_ok=True)
    if args.command == "execute":
        with writer_lock(args.work / "execution-lock"):
            result = execute(args.work, args.source, args.project, args.analysis_suffix)
    else:
        result = recipe(args.work, args.source, args.analysis_suffix)
    if args.command == "plan":
        atomic_json(args.work / "analysis-rebuild-plan.json", result)
        rebuilt_policy(args.source, args.work, result)
    print(json.dumps(result, ensure_ascii=False))
    if args.command == "execute" and result["status"] != "completed":
        raise SystemExit(1)
