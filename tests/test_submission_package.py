import copy
import csv
import gzip
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from nutrigenomics import submission_package as pkg


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pkg.canonical(value), encoding="utf-8")


def table(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def fixture(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    code, producer = tmp_path / "code/code.zip", tmp_path / "producer/producer.zip"
    script = b"# exact source bytes\n"
    script_sha = __import__("hashlib").sha256(script).hexdigest()
    for name in [code, producer]:
        name.parent.mkdir()
        with zipfile.ZipFile(name, "w") as archive:
            archive.writestr("code/scripts/build_original.py", script)
    record = {"id": "N06", "source_refs": [{"url": "https://www.ncbi.nlm.nih.gov/geo/"}], "publication_review_status": "pending"}
    run = {"series_accession": "GSE1", "analysis_version": "v1", "source_script_sha256": script_sha}
    table(release / "tables/science_record.csv.gz", [
        {"id": "N06", "kind": "study", "metadata_json": pkg.canonical(record)},
        {"id": "1", "kind": "analysis_run", "metadata_json": pkg.canonical(run)}])
    table(release / "tables/effect_selector.csv.gz", [{"series_accession": "GSE1", "contrast": "a", "analysis_version": "v1", "feature_type": "cpg", "total": "2"}])
    part = pkg.digest(["GSE1", "v1"])[:16]
    (release / "effects").mkdir()
    for extension in [".parquet", ".csv.gz"]:
        (release / ("effects/" + part + extension)).write_bytes(b"immutable effect values")
    write(release / "source-catalog.json", [
        {"id": "allowed", "license": "CC-BY-4.0", "export_allowed": True, "url": "https://example.org/terms"},
        {"id": "CTD:x", "license": "redistribution-unconfirmed", "export_allowed": False, "url": "https://ctdbase.org/"}])
    write(release / "rights-policy.json", {"analyses": [{"id": "rights:v1", "series_accession": "GSE1", "analysis_version": "v1", "own_results_license": "CC-BY-4.0", "export_allowed": True, "terms_url": "https://www.ncbi.nlm.nih.gov/geo/info/disclaimer.html"}]})
    write(release / "dataset-metadata.json", {"release_id": "public-v2", "blanket_third_party_license": False, "counts": {"effect": 2}})
    manifest = {"release_id": "public-v2", "builder_sha256": script_sha,
        "candidate_layers": {"literature": {"withheld_assertions": 350}}, "files": []}
    for path in sorted(release.rglob("*")):
        if path.is_file():
            manifest["files"].append({"path": path.relative_to(release).as_posix(), "bytes": path.stat().st_size, "sha256": pkg.sha256(path)})
    write(release / "manifest.json", manifest)
    receipt = tmp_path / "receipt.json"
    write(receipt, {"passed": True, "complete": True, "all_manifest_files_hash_verified": True, "source_files_unchanged": True,
        "public_release_id": "public-v2", "exact_code_archive_sha256": pkg.sha256(code),
        "source_fingerprints_after": {str(p.resolve()): pkg.fingerprint(p) for p in release.rglob("*") if p.is_file()}})
    dictionary = tmp_path / "dictionary.json"
    write(dictionary, {"release_id": "public-v2", "tables": {"effect": [{"name": "id", "type": "TEXT"}]}})
    return {"release": release, "code_archive": code, "original_builder_archive": producer, "dictionary": dictionary,
        "prior_verification": receipt, "expected_prior_sha256": pkg.sha256(receipt),
        "expected_manifest_sha256": pkg.sha256(release / "manifest.json"),
        "expected_builder_archive_sha256": pkg.sha256(producer), "output": tmp_path / "package"}


def proposal(record):
    updated = copy.deepcopy(record)
    updated["source_refs"][0]["url"] = "https://pubmed.ncbi.nlm.nih.gov/21429719/"
    return {"automatic_expert_approval": False, "patches": [{
        "record_kind": "study", "record_id": "N06", "old_record_sha256": pkg.digest(record), "new_record_sha256": pkg.digest(updated),
        "classification": "mechanical_source_link", "changes": [{"path": ["source_refs", 0, "url"], "old_value": record["source_refs"][0]["url"], "new_value": updated["source_refs"][0]["url"], "operation": "replace"}],
        "proof_ids": ["pmid"], "reason": "Official PMID source-link correction", "proposed_record": updated, "expert_review_status": "pending"}]}


def test_prepare_portable_references_and_human_gate(fixture):
    result = pkg.prepare(**fixture)
    output = fixture["output"]
    assert result["machine_package_integrity"] is True
    assert result["publication_ready"] is False
    filemap = json.loads((output / "filemap.json").read_text())
    assert not pkg.PRIVATE.search(pkg.canonical(filemap))
    assert not (output / "base").exists()  # prepare never copies matrices
    catalog = json.loads((output / "effect-catalog.json").read_text())
    assert catalog["five_part_effect_key"] == pkg.KEY_FIELDS
    assert catalog["effect_values_duplicated"] == 0
    assert all(p["path"].startswith("base/effects/") for p in catalog["selectors"][0]["files"])
    rights = json.loads((output / "rights-machine-audit.json").read_text())
    assert rights["human_final_rights_approval"] == "pending"
    assert rights["excluded_value_sources"][0]["machine_decision"] == "exclude_values_keep_citation_only"
    assert json.loads((output / "zenodo-base.draft.json").read_text())["metadata"]["license"] is None
    jsonl = [json.loads(line) for line in (output / "science-records.jsonl").read_text(encoding="utf-8").splitlines()]
    with sqlite3.connect(output / "release-science.sqlite3") as db:
        actual = {(kind, record_id): (json.loads(payload), sha) for kind, record_id, payload, sha in db.execute("SELECT kind,id,metadata_json,record_sha256 FROM science_record")}
        assert actual == {(r["kind"], r["id"]): (r["record"], r["record_sha256"]) for r in jsonl}
        assert db.execute("SELECT sum(total) FROM effect_catalog").fetchone()[0] == 2
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='effect'").fetchall()


@pytest.mark.parametrize("defect", ["manifest", "missing", "same_size", "nonempty_wal", "producer", "dictionary", "rights", "private"])
def test_prepare_rejects_concrete_faults(fixture, defect):
    release = fixture["release"]
    if defect == "manifest":
        (release / "manifest.json").write_text("{}")
    elif defect == "missing":
        (release / "tables/effect_selector.csv.gz").unlink()
    elif defect == "same_size":
        p = release / "tables/effect_selector.csv.gz"
        p.write_bytes(b"x" * p.stat().st_size)
    elif defect == "nonempty_wal":
        Path(str(release / "manifest.json") + "-wal").write_bytes(b"page")
    elif defect == "producer":
        with zipfile.ZipFile(fixture["original_builder_archive"], "w") as z:
            z.writestr("code/unrelated.py", b"unrelated")
        fixture["expected_builder_archive_sha256"] = pkg.sha256(fixture["original_builder_archive"])
    elif defect == "dictionary":
        write(fixture["dictionary"], {"release_id": "different"})
    else:
        # Modify a source and then emulate a new trusted receipt: the deeper
        # content/rights check still rejects invalid publication inputs.
        name = "source-catalog.json" if defect == "rights" else "tables/science_record.csv.gz"
        if defect == "rights":
            write(release / name, [{"id": "x", "export_allowed": True, "license": "unknown"}])
        else:
            table(release / name, [{"id": "N06", "kind": "study", "metadata_json": pkg.canonical({"path": "/home/private/input.tsv"})}])
        manifest = json.loads((release / "manifest.json").read_text())
        row = next(r for r in manifest["files"] if r["path"] == name)
        row.update(bytes=(release / name).stat().st_size, sha256=pkg.sha256(release / name))
        write(release / "manifest.json", manifest)
        fixture["expected_manifest_sha256"] = pkg.sha256(release / "manifest.json")
        receipt = json.loads(fixture["prior_verification"].read_text())
        receipt["source_fingerprints_after"] = {str(p.resolve()): pkg.fingerprint(p) for p in release.rglob("*") if p.is_file()}
        write(fixture["prior_verification"], receipt)
        fixture["expected_prior_sha256"] = pkg.sha256(fixture["prior_verification"])
    with pytest.raises(ValueError):
        pkg.prepare(**fixture)
    assert not fixture["output"].exists()


def test_hash_bound_source_patch_preserves_pending():
    old = {"id": "N06", "source_refs": [{"url": "https://www.ncbi.nlm.nih.gov/geo/"}], "publication_review_status": "pending"}
    plan = proposal(old)
    records, ledger = pkg.apply_corrections([{"id": "N06", "kind": "study", "record": old}], plan, {"pmid": {"url": "https://pubmed.ncbi.nlm.nih.gov/21429719/"}})
    assert records[0]["record"]["publication_review_status"] == "pending"
    assert ledger[0]["old_record_sha256"] != ledger[0]["new_record_sha256"]
    assert old["source_refs"][0]["url"].endswith("geo/")  # immutable source unchanged


@pytest.mark.parametrize("defect", ["old_hash", "new_hash", "old_value", "proof", "approval", "numeric"])
def test_patch_rejects_unbound_or_nonmechanical_changes(defect):
    old = {"id": "N06", "source_refs": [{"url": "https://www.ncbi.nlm.nih.gov/geo/"}], "publication_review_status": "pending", "effect": 1.2}
    plan = proposal(old)
    patch = plan["patches"][0]
    proofs = {"pmid": {"url": "https://pubmed.ncbi.nlm.nih.gov/21429719/"}}
    if defect.endswith("hash"):
        patch[defect.replace("hash", "record_sha256")] = "0" * 64
    elif defect == "old_value":
        patch["changes"][0]["old_value"] = "wrong"
    elif defect == "proof":
        proofs = {}
    elif defect == "approval":
        patch["changes"][0]["path"] = ["publication_review_status"]
    else:
        patch["changes"][0].update(path=["effect"], old_value=1.2, new_value=2.4)
    with pytest.raises(ValueError):
        pkg.apply_corrections([{"kind": "study", "id": "N06", "record": old}], plan, proofs)


def test_assembly_full_hash_and_atomic_publication(fixture):
    result = pkg.prepare(**fixture)
    roots = {"public_base": str(fixture["release"]), "code": str(fixture["code_archive"].parent), "producer": str(fixture["original_builder_archive"].parent)}
    destination = fixture["output"].parent / "assembled"
    receipt = pkg.assemble(fixture["output"], result["filemap_sha256"], roots, destination)
    assert receipt["complete"]
    assert (destination / "ASSEMBLY.json").exists()
    assert (destination / "base/manifest.json").read_bytes() == (fixture["release"] / "manifest.json").read_bytes()


def test_partial_copy_never_becomes_publishable(fixture, monkeypatch):
    result = pkg.prepare(**fixture)
    def partial(source, dest, length):
        dest.write(source.read(1))
    monkeypatch.setattr(pkg.shutil, "copyfileobj", partial)
    destination = fixture["output"].parent / "assembled"
    roots = {"public_base": str(fixture["release"]), "code": str(fixture["code_archive"].parent), "producer": str(fixture["original_builder_archive"].parent)}
    with pytest.raises(ValueError, match="Partial"):
        pkg.assemble(fixture["output"], result["filemap_sha256"], roots, destination)
    assert not destination.exists()
    assert list(destination.parent.glob(".assembled.partial-*/FAILED.json"))


def test_assembly_manifest_tampering_rejected(fixture):
    result = pkg.prepare(**fixture)
    (fixture["output"] / "filemap.json").write_text("{}")
    with pytest.raises(ValueError, match="manifest changed"):
        pkg.assemble(fixture["output"], result["filemap_sha256"], {}, fixture["output"].parent / "assembled")


def test_source_change_during_prepare_prevents_finalization(fixture, monkeypatch):
    original = pkg._write
    def mutate_after_status(path, value):
        original(path, value)
        if path.name == "package-status.json":
            (fixture["release"] / "tables/effect_selector.csv.gz").write_bytes(b"changed")
    monkeypatch.setattr(pkg, "_write", mutate_after_status)
    with pytest.raises(ValueError, match="changed"):
        pkg.prepare(**fixture)
    assert not fixture["output"].exists()


def test_new_workflow_archive_is_distinct_and_exact(fixture):
    reader = fixture["output"].parent / "reader.zip"
    with zipfile.ZipFile(reader, "w") as archive:
        archive.writestr("code/nutrigenomics/submission_package.py", Path(pkg.__file__).read_bytes())
    result = pkg.prepare(**fixture, current_reader_archive=reader, expected_reader_sha256=pkg.sha256(reader))
    assert result["current_workflow_archive_bound"] is True
    provenance = json.loads((fixture["output"] / "producer-provenance.json").read_text())
    assert provenance["exact_legacy_runtime_code_sha256"] != provenance["current_workflow_code_sha256"]


def test_new_workflow_without_exact_producer_bytes_rejected(fixture):
    reader = fixture["output"].parent / "reader.zip"
    with zipfile.ZipFile(reader, "w") as archive:
        archive.writestr("code/unrelated.py", b"not this producer")
    with pytest.raises(ValueError, match="exact curation producer"):
        pkg.prepare(**fixture, current_reader_archive=reader, expected_reader_sha256=pkg.sha256(reader))


def test_unused_denied_policy_is_preserved_as_exclusion():
    policy = {"analyses": [{"series_accession": "E-MTAB-12527", "analysis_version": "not_public", "export_allowed": False, "terms_url": "https://www.ebi.ac.uk/biostudies/"}]}
    audit, permissions = pkg.rights_audit([], policy, {})
    assert permissions == {}
    assert audit["excluded_quantitative_analysis_policies"][0]["machine_decision"] == "exclude_quantitative_results"


@pytest.mark.parametrize("tamper", [False, True])
def test_source_method_annotation_is_additive_only(tamper):
    old = {"id": "S05", "metadata": {"model": "paired", "effect": 1.2}, "publication_review_status": "pending"}
    new = copy.deepcopy(old)
    new["metadata"]["source_method_notes"] = [{"id": "extraction", "actor": "AI_precheck", "source_url": "https://doi.org/example", "source_locator": "Methods Sec22", "source_file_sha256": "a" * 64, "source_fact": "Baseline and follow-up extraction methods differ", "interpretation_limit": "Diet/time and extraction method separation not established", "expert_review_status": "pending", "scientific_approval": False}]
    if tamper:
        new["metadata"]["effect"] = 2.4
    patch = {"record_kind": "study", "record_id": "S05", "old_record_sha256": pkg.digest(old), "new_record_sha256": pkg.digest(new), "classification": "additive_source_method_annotation", "proof_ids": ["source"], "changes": [{"operation": "replace", "path": ["metadata"], "old_value": old["metadata"], "new_value": new["metadata"]}], "proposed_record": new, "expert_review_status": "pending", "reason": "Source-checked method limitation only"}
    args = ([{"kind": "study", "id": "S05", "record": old}], {"automatic_expert_approval": False, "patches": [patch]}, {"source": {"url": "https://doi.org/example"}})
    if tamper:
        with pytest.raises(ValueError, match="every existing"):
            pkg.apply_corrections(*args)
    else:
        records, ledger = pkg.apply_corrections(*args)
        assert records[0]["record"]["metadata"]["effect"] == 1.2
        assert ledger[0]["expert_review_status"] == "pending"


def test_private_proof_locator_never_enters_public_artifact(fixture):
    with pytest.raises(ValueError, match="Private operational"):
        pkg.prepare(**fixture, proofs={"source": {"url": "https://example.org/", "operator_path": "/home/private/source.xml"}})
    assert not fixture["output"].exists()
