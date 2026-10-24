"""Export public development material and official dictionaries; never credentials.

Run on the existing development host. This does not change frozen v6 results.
"""
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def export():
    from nutri_literature.db import dumps, digest
    from nutri_literature.settings import Settings
    from nutri_literature import benchmarks

    home = ROOT / "runtime"
    settings = Settings(home, ROOT / "config/topics.json")
    index = json.loads((home / "public_benchmarks/v2-measurement-index.json").read_bytes())
    item = index["steps"]["prepare:development:bioredirect:abstract"]
    inputs_path = Path(item["inputs_path"])
    inputs, receipt, input_hash = benchmarks._load_inputs(settings, inputs_path)
    if inputs.get("split") != "development" or len(inputs["documents"]) != 100:
        raise ValueError("Only the existing 100-paper development cohort may be exported")
    upstream = home / "v7/frozen-v6-upstream"
    gene_pointer = json.loads((upstream / "gene-snapshot.json").read_bytes())
    chemical_pointer = json.loads((upstream / "mesh-snapshot.json").read_bytes())
    gene_path = upstream / "genes" / gene_pointer["filename"]
    chemical_path = upstream / "mesh" / chemical_pointer["filename"]
    gene_raw, chemical_raw = gene_path.read_bytes(), chemical_path.read_bytes()
    genes, chemicals = json.loads(gene_raw), json.loads(chemical_raw)
    if digest(dumps(genes["records"])) != genes["manifest"]["records_sha256"]:
        raise ValueError("Gene dictionary hash mismatch")
    if digest(chemical_raw) != chemical_pointer["sha256"]:
        raise ValueError("Chemical dictionary hash mismatch")
    records = [
        {"kind": "gene", "native_id": r["native_id"], "taxon": r["taxon_id"],
         "names": sorted(set([r["symbol"], r["name"], *r["aliases"]]))}
        for r in genes["records"]
    ]
    records.extend(
        {"kind": "chemical", "native_id": r["native_id"], "names": r["terms"]}
        for r in chemicals["records"] if r["chemical_candidate"]
    )
    export_dir = ROOT / "runtime/chatgpt-export"
    export_dir.mkdir(parents=True, exist_ok=True)
    dictionary = export_dir / "official-id-dictionary.json.gz"
    content = dumps({"schema_version": "literature-chatgpt-id-dictionary-v1",
        "source_references": [genes["manifest"], chemicals["manifest"]],
        "source_sha256": {"gene": digest(gene_raw), "chemical": digest(chemical_raw)},
        "records": records}).encode()
    with dictionary.open("wb") as handle:
        handle.write(gzip.compress(content, mtime=0))
    with sqlite3.connect(settings.database) as con:
        rows = con.execute("SELECT * FROM audit WHERE kind='public_corpus_prepared'").fetchall()
    public_audit = export_dir / "public-audit.json"
    public_audit.write_text(dumps(rows), encoding="utf-8")
    baseline = home / "v6/refinement-r4/relations-end_to_end"
    output = ROOT / "dist/chatgpt-development-inputs.tar.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as archive:
        for filename in ("inputs.json", "gold.json", "manifest.json"):
            archive.add(inputs_path.parent / filename, arcname="public_benchmarks/bioredirect/" + inputs_path.parent.name + "/" + filename)
        for doc in inputs["documents"]:
            filename = "paper-" + doc["pmid"] + ".json"
            raw = (baseline / filename).read_bytes()
            if json.loads(raw)["source_hash"] != doc["source_hash"]:
                raise ValueError("Baseline source mismatch")
            archive.add(baseline / filename, arcname="baseline/" + filename)
        archive.add(baseline / "report.json", arcname="baseline/report.json")
        archive.add(public_audit, arcname="public-audit.json")
        archive.add(dictionary, arcname=dictionary.name)
    report = {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "papers": len(inputs["documents"]), "input_sha256": input_hash,
              "dictionary_records": len(records), "contains_credentials": False,
              "contains_protected_test": False}
    output.with_suffix(".manifest.json").write_text(dumps(report), encoding="utf-8")
    print(dumps(report))


if __name__ == "__main__":
    export()
