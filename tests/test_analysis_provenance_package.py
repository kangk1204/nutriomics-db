import json

from nutrigenomics.explorer_build import sha256
from scripts.package_analysis_provenance import package


def test_provenance_keeps_sample_roles_and_original_hash_without_raw_blobs(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    sample = project / "sample_metadata.tsv"
    sample.write_text("sample_accession\tsubject_id\ttimepoint\tcharacteristics_json\nGSM1\tP1\t0\tprivate blob\nGSM2\tP1\t6\tprivate blob\n")
    metadata = project / "analysis_metadata.json"
    metadata.write_text(json.dumps({"design_formula": "~condition+person", "independent_people": 1,
                                    "source_file": "/home/research/private/input.tsv"}))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"runs": [{"series_accession": "GSE1", "analysis_version": "v1",
        "output_manifest_json": json.dumps([{"path": str(p), "sha256": sha256(p)} for p in (sample, metadata)])}]}))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"analyses": [{"id": "own", "series_accession": "GSE1",
                                               "analysis_version": "v1", "export_allowed": True}]}))
    release = tmp_path / "release"
    release.mkdir()
    (release / "manifest.json").write_text(json.dumps({"release_id": "r", "database_sha256": "a" * 64}))
    output = tmp_path / "supplement"
    result = package(audit, policy, release, project, output)
    assert result["files"] == 2 and result["scientific_review"] == "pending"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    design = next(r for r in manifest["files"] if r["role"] == "sample_design")
    assert design["rows"] == 2 and design["original_sha256"] == sha256(sample)
    assert "characteristics_json" in design["excluded_columns"]
    content = (output / design["path"]).read_text(encoding="utf-8")
    assert "GSM2\tP1\t6" in content and "private blob" not in content
    params = next(r for r in manifest["files"] if r["role"] == "analysis_metadata")
    values = json.loads((output / params["path"]).read_text(encoding="utf-8"))
    assert values == {"design_formula": "~condition+person", "independent_people": 1}
    assert not output.with_name(output.name + ".building").exists()

