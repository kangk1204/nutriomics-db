"""Audit the new ICO cohort before fitting; retain confirmed PMC-only sources."""

import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from defusedxml import ElementTree
    from nutri_literature import corpora_v6
    from nutri_literature.benchmarks import _write_immutable
    from nutri_literature.clinical_identity_v6 import auditable_identity
    from nutri_literature.db import digest, dumps

    if str(ROOT) != "/home/keunsoo/projects/nutriomics-literature-v6-development":
        raise ValueError("Only the isolated Linux v6 checkout may prepare this cohort")
    runtime = ROOT / "runtime"
    folder = runtime / "v6/training-data/recovery"
    raw_inputs = json.loads((folder / "inference-dev/inputs.json").read_bytes())
    old_spec = json.loads((folder / "inference-dev/spec.json").read_bytes())
    destination = folder / "inference-dev-confirmed"
    destination.mkdir(parents=True, exist_ok=True)
    for doc in raw_inputs["documents"]:
        if doc["pmid_mapping_known"] is True:
            continue
        pmcid = doc["pmcid"]
        source = folder / ("inference-idmap/europepmc-" + pmcid + ".response")
        if not source.exists():
            import requests

            response = requests.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                                    params={"query": pmcid, "format": "json"}, timeout=(15, 60))
            response.raise_for_status()
            source.write_bytes(response.content)
        target = destination / ("europepmc-" + pmcid + ".json")
        if not target.exists():
            shutil.copyfile(source, target)
        nlm = None
        for path in (folder / "inference-idmap").glob("elink-*.xml"):
            root = ElementTree.fromstring(path.read_bytes(), forbid_dtd=False,
                                          forbid_entities=True, forbid_external=True)
            links = [r for r in root.findall("LinkSet") if r.findtext("IdList/Id") == pmcid[3:]]
            if len(links) == 1 and not links[0].findall("LinkSetDb"):
                nlm = path
                break
        if nlm is None:
            raise ValueError("Unresolved PMID cannot be treated as confirmed PMC-only")
        link = destination / ("ncbi-" + pmcid + ".xml")
        if not link.exists():
            shutil.copyfile(nlm, link)
        doc["identifier_resolution"] = "confirmed_pmc_only"
        doc["pmc_only_receipts"] = {
            "ncbi": {"url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
                     "filename": link.name, "sha256": digest(link.read_bytes())},
            "europepmc": {"url": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                          "filename": target.name, "sha256": digest(target.read_bytes())}}
        if not auditable_identity(doc, destination):
            raise ValueError("Official sources do not confirm PMID absence")
    inputs = destination / "inputs.json"
    if not inputs.exists():
        _write_immutable(inputs, raw_inputs)
    spec = {**old_spec, "inputs_path": str(inputs), "inputs_sha256": digest(inputs.read_bytes()),
            "pmc_only_policy": "PMID remains unknown; official NCBI no-link + EuropePMC PMC-only receipts",
            "confirmed_pmc_only_articles": sum(d.get("identifier_resolution") == "confirmed_pmc_only"
                                                for d in raw_inputs["documents"])}
    if not (destination / "spec.json").exists():
        _write_immutable(destination / "spec.json", spec)
    release = runtime / "v6/clinical-base-sources-recovery/evidenceinference.tar.gz"
    train_raw = folder / "inference-train-raw.json"
    if not train_raw.exists():
        corpora_v6.inference_train(release, train_raw, folder / "inference-idmap/id-map.json")
    value = json.loads(train_raw.read_bytes())
    mapped = folder / "inference-train-mapped.json"
    if not mapped.exists():
        _write_immutable(mapped, {**value, "documents": [d for d in value["documents"] if d["pmid_mapping_known"]],
                                 "unmapped_training_pmcids_excluded": [d["pmcid"] for d in value["documents"]
                                                                       if not d["pmid_mapping_known"]]})
    old = runtime / "v6/training-data/outcome-audited-safe"
    prep = json.loads((runtime / "v6/data-preparation.json").read_bytes())
    original = ROOT / Path(prep["audited_directory"].replace("\\", "/"))
    relation = folder / "relation-evidence-audited"
    if not (relation / "audit.json").exists():
        corpora_v6.audit_training(original / "relation-train.json", [inputs], relation,
                                 prior_audit=old / "audit.json")
    for kind, source in (("evidence", mapped), ("outcome", old / "relation-train.json")):
        out = folder / (kind + "-evidence-audited")
        if not (out / "audit.json").exists():
            corpora_v6.audit_training(source, [], out, prior_audit=relation / "audit.json")
    models = runtime / "v6/models-evidence"
    models.mkdir(parents=True, exist_ok=True)
    for name in ("gene", "genes", "gene-snapshot.json", "mesh", "mesh-snapshot.json", "learned-v5"):
        link = models / name
        if not link.exists():
            link.symlink_to(runtime / "v6/models" / name)
    features = runtime / "v6/features-evidence"
    features.mkdir(parents=True, exist_ok=True)
    for path in (runtime / "v6/features").glob("*.json"):
        if path.name != "manifest.json" and not (features / path.name).exists():
            shutil.copyfile(path, features / path.name)
    sources = runtime / "v6/clinical-base-sources-evidence"
    sources.mkdir(parents=True, exist_ok=True)
    for name in ("rct-train.txt", "rct-dev.txt", "rct-test.txt", "ebm_nlp_2_00.tar.gz"):
        path = sources / name
        if not path.exists():
            path.symlink_to(runtime / "v6/clinical-base-sources-recovery" / name)
    record = {"models": str(models), "features": str(features), "sources": str(sources),
              "clinical_spec": str(destination / "spec.json"), "relation": str(relation),
              "outcome": str(folder / "outcome-evidence-audited"),
              "evidence": str(folder / "evidence-evidence-audited"),
              "confirmed_pmc_only_articles": spec["confirmed_pmc_only_articles"],
              "excluded_unmapped_training_articles": sum(not d["pmid_mapping_known"] for d in value["documents"]),
              "sealed_final_opened": False}
    output = runtime / "v6/evidence-preparation.json"
    if not output.exists():
        _write_immutable(output, record)
    print(dumps(record))


if __name__ == "__main__":
    main()
