"""Recover audited inputs on the approved alternate CPU host; no sealed scoring."""

import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from nutri_literature import corpora_v6
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.candidates_v4 import build_gene_snapshot, directory_hash
    from nutri_literature.db import connect, digest, dumps, initialize
    from nutri_literature.settings import Settings
    from nutri_literature.training_v4 import rct_documents
    import requests

    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Recovery is restricted to the isolated Linux v6 checkout")
    settings = Settings(ROOT / "runtime", ROOT / "config/topics.json")
    initialize(settings)
    models = settings.home / "v6/models"
    models.mkdir(parents=True, exist_ok=True)
    gene = models / "gene/selected_model"
    if not gene.exists():
        shutil.copytree(settings.home / "v4/models/gene/selected_model", gene)
    if directory_hash(gene) != "fadb61f1007093cda755c9160db2b584d321d926a9fad3e1ba2f7782342dcf77":
        raise ValueError("Recovered BC2GM directory differs from the frozen encoder")
    with connect(settings, durable=True) as connection:
        for row in json.loads((settings.home / "v6/validation/public-audit-export.json").read_bytes()):
            if row["kind"] not in {"public_corpus_prepared", "public_partition_predeclared", "public_protected_test_ids"}:
                raise ValueError("Only public-source receipts may be recovered")
            connection.execute("INSERT OR IGNORE INTO audit VALUES(?,?,?,?,?)", tuple(row.values()))
    steps = {}
    for manifest in (settings.home / "public_benchmarks").glob("*/*/manifest.json"):
        record = json.loads(manifest.read_bytes())
        if record["split"] != "development":
            raise ValueError("Only existing development benchmarks may be restored")
        for name, field in (("inputs.json", "inputs_sha256"), ("gold.json", "gold_sha256"),
                            ("oracle_entities.json", "oracle_entities_sha256")):
            if digest((manifest.parent / name).read_bytes()) != record[field]:
                raise ValueError("Recovered development benchmark hash differs")
        level = next(iter(record["source_levels"]))
        steps["prepare:development:" + record["corpus"] + ":" + level] = {
            "inputs_path": str(manifest.parent / "inputs.json"),
            "gold_path": str(manifest.parent / "gold.json"),
            "oracle_entities_path": str(manifest.parent / "oracle_entities.json"),
        }
    index = settings.home / "public_benchmarks/v2-measurement-index.json"
    if not index.exists():
        _write_immutable(index, {"steps": steps, "recovered_development_inputs": True})
    historical = json.loads((settings.home / "v6/validation/model-manifests-v5-recovery.json").read_bytes())
    sources = settings.home / "v6/clinical-base-sources-recovery"
    sources.mkdir(parents=True, exist_ok=True)

    def download(url, path, expected=None):
        if not path.exists():
            temporary = path.with_suffix(".download")
            with requests.get(url, stream=True, timeout=(15, 180)) as response:
                response.raise_for_status()
                with temporary.open("wb") as stream:
                    for block in response.iter_content(1048576):
                        stream.write(block)
            temporary.replace(path)
        sha = digest(path.read_bytes())
        if expected and sha != expected:
            raise ValueError("Official training release changed: " + path.name)
        receipt = path.with_suffix(path.suffix + ".receipt.json")
        if not receipt.exists():
            _write_immutable(receipt, {"url": url, "sha256": sha})

    for split in ("train", "dev", "test"):
        download("https://raw.githubusercontent.com/Franck-Dernoncourt/pubmed-rct/master/PubMed_20k_RCT/" + split + ".txt",
                 sources / ("rct-" + split + ".txt"),
                 historical["clinical"]["source_hashes"].get("rct-" + split + ".txt"))
    download("https://raw.githubusercontent.com/bepnye/EBM-NLP/master/ebm_nlp_2_00.tar.gz",
             sources / "ebm_nlp_2_00.tar.gz", historical["clinical"]["source_hashes"]["ebm_nlp_2_00.tar.gz"])
    release = sources / "evidenceinference.tar.gz"
    download("https://evidence-inference.ebm-nlp.com/v2.0.tar.gz", release)
    data = settings.home / "v6/training-data/recovery"
    data.mkdir(parents=True, exist_ok=True)
    idmap = data / "inference-idmap/id-map.json"
    if not idmap.exists():
        corpora_v6.inference_idmap(release, idmap.parent)
    dev = data / "inference-dev"
    if not (dev / "spec.json").exists():
        corpora_v6.inference_development(release, dev, idmap)
    dev_input = json.loads((dev / "inputs.json").read_bytes())
    if any(doc["pmid_mapping_known"] is not True for doc in dev_input["documents"]):
        raise ValueError("Evidence Inference development still has unmapped PMCID; resolve before fitting")
    training = data / "inference-train-raw.json"
    if not training.exists():
        corpora_v6.inference_train(release, training, idmap)
    raw_train = json.loads(training.read_bytes())
    mapped = data / "inference-train-mapped.json"
    if not mapped.exists():
        kept = [d for d in raw_train["documents"] if d["pmid_mapping_known"] is True]
        _write_immutable(mapped, {**raw_train, "documents": kept,
                                "unmapped_training_pmcids_excluded": [d["pmcid"] for d in raw_train["documents"]
                                                                      if d["pmid_mapping_known"] is not True]})
    protected = [dev / "inputs.json"]
    for split in ("dev", "test"):
        path = data / ("rct-" + split + "-source-only.json")
        if not path.exists():
            docs = [{"pmid": d["pmid"], "text": "\n".join(s["text"] for s in d["sentences"])}
                    for d in rct_documents(sources / ("rct-" + split + ".txt"))]
            _write_immutable(path, {"split": "development" if split == "dev" else "test", "documents": docs})
        protected.append(path)
    preparation = json.loads((settings.home / "v6/data-preparation.json").read_bytes())
    original = ROOT / Path(preparation["audited_directory"].replace("\\", "/"))
    relation_dir = data / "relation-audited"
    if not (relation_dir / "audit.json").exists():
        corpora_v6.audit_training(original / "relation-train.json", protected, relation_dir,
                                 prior_audit=original / "audit.json")
    outcome_raw = data / "outcome-raw.json"
    if not outcome_raw.exists():
        corpora_v6.outcome_train(sources / "ebm_nlp_2_00.tar.gz", outcome_raw)
    for kind, path in (("outcome", outcome_raw), ("evidence", mapped)):
        destination = data / (kind + "-audited")
        if not (destination / "audit.json").exists():
            corpora_v6.audit_training(path, [], destination, prior_audit=relation_dir / "audit.json")
    if not (models / "gene-snapshot.json").exists():
        snapshot = build_gene_snapshot(models / "genes")
        _write_immutable(models / "gene-snapshot.json", {"filename": snapshot.name,
                                                        "sha256": digest(snapshot.read_bytes())})
    record = {
        "server": "100.84.183.6", "sources": str(sources), "data": str(data),
        "relation_audit": str(relation_dir / "audit.json"),
        "relation_train": str(relation_dir / "relation-train.json"),
        "models": str(models), "sealed_final_opened": False,
        "original_server_results_unavailable": True,
        "legacy_v5_full_model_restored": False,
    }
    destination = settings.home / "v6/recovery-preparation.json"
    if not destination.exists():
        _write_immutable(destination, record)
    print(dumps(record))


if __name__ == "__main__":
    main()
