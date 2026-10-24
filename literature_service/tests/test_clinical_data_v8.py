"""Small native annotation fixtures, never public held-out labels."""
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from nutri_literature.clinical_data_v8 import prepare_clinical


def filehash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, bad_label=False, missing=False, duplicate=False):
    source = tmp_path / "sources"
    source.mkdir()
    members = {
        "ebm/documents/1.text": "Adults β received DHA",
        "ebm/documents/1.tokens": "Adults β received DHA",
        "ebm/annotations/aggregated/starting_spans/participants/train/1.AGGREGATED.ann": "1 1 0 0",
        "ebm/annotations/aggregated/starting_spans/interventions/train/1.AGGREGATED.ann": "0 0 0 1",
        "ebm/annotations/aggregated/starting_spans/outcomes/train/1.AGGREGATED.ann": "0 0 1 0",
        # Deliberately malformed protected labels must never be opened/parsed.
        "ebm/annotations/aggregated/starting_spans/outcomes/train/2.AGGREGATED.ann": "DO NOT READ",
        "ebm/annotations/aggregated/starting_spans/outcomes/test/99.AGGREGATED.ann": "DO NOT READ",
    }
    if bad_label:
        members["ebm/annotations/aggregated/starting_spans/outcomes/train/1.AGGREGATED.ann"] = "0 0 UNKNOWN 0"
    if missing:
        del members["ebm/documents/1.tokens"]
    with tarfile.open(source / "ebm_nlp_2_00.tar.gz", "w:gz") as tar:
        for name, text in members.items():
            raw = text.encode()
            item = tarfile.TarInfo(name)
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
        if duplicate:
            raw = b"1 1 0 0"
            item = tarfile.TarInfo("ebm/annotations/aggregated/starting_spans/participants/train/1.AGGREGATED.ann")
            item.size = len(raw)
            tar.addfile(item, io.BytesIO(raw))
    (source / "rct-train.txt").write_text("###3\nMETHODS\tAdults β received DHA.\nRESULTS\tPressure declined.\n\n###4\nUNKNOWN\tProtected source should not be parsed.\n", encoding="utf-8")
    manifest = tmp_path / "clinical-base.json"
    manifest.write_text(json.dumps({"training_split": "train", "test_used_for_selection": False,
        "training_pmids": {"ebm": ["1"], "rct": ["3"]}, "excluded_pmids": {"ebm": ["2"], "rct": ["4"]},
        "source_hashes": {name: filehash(source / name) for name in ("rct-train.txt", "ebm_nlp_2_00.tar.gz")}}), encoding="utf-8")
    return source, manifest, tmp_path / "prepared"


def test_native_spans_roles_hashes_and_no_protected_member_reads(tmp_path, monkeypatch):
    source, allowed, output = fixture(tmp_path)
    original = tarfile.TarFile.extractfile
    opened = []

    def read_member(self, member):
        name = member.name
        assert "/test/" not in name and not name.endswith("/2.AGGREGATED.ann")
        opened.append(name)
        return original(self, member)
    monkeypatch.setattr(tarfile.TarFile, "extractfile", read_member)
    result = prepare_clinical(source, allowed, output, allowed_manifest_sha256=filehash(allowed))
    assert result["training_documents"] == {"ebm": 1, "rct": 1} and result["passed"]
    training = json.loads((output / "data/clinical-train.json").read_text(encoding="utf-8"))
    document = training["documents"][0]
    assert document["text"] == "Adults β received DHA"
    for span in document["annotated_spans"]:
        assert document["text"][span["start"]:span["end"]] == span["text"]
    assert {s["label"] for s in document["annotated_spans"]} == {"participants", "interventions", "outcomes"}
    audit = json.loads((output / "data/clinical-audit.json").read_text())
    assert audit["audited_training_sha256"] == filehash(output / "data/clinical-train.json")
    assert audit["protected_overlap"] == 0 and audit["protected_pmids_checked"] == 2
    assert not audit["evidence_inference_ico_converted_to_bio"]
    rct = json.loads((output / "data/rct-train.json").read_text(encoding="utf-8"))["documents"][0]
    assert [s["label"] for s in rct["sentences"]] == ["METHODS", "RESULTS"]
    assert len(opened) == 5
    with pytest.raises(FileExistsError):
        prepare_clinical(source, allowed, output)


@pytest.mark.parametrize("kind", ["bad_label", "missing", "duplicate"])
def test_unknown_annotation_missing_and_duplicate_fail_without_frozen_output(tmp_path, kind):
    source, allowed, output = fixture(tmp_path, **{kind: True})
    with pytest.raises(ValueError):
        prepare_clinical(source, allowed, output)
    assert not output.exists()


def test_actual_id_and_normalized_text_protection_overlap_fail(tmp_path):
    source, allowed, output = fixture(tmp_path)
    protection = tmp_path / "protection.json"
    protection.write_text(json.dumps({"source_only": True, "pmids": ["1"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="overlap"):
        prepare_clinical(source, allowed, output, protection)
    textkey = hashlib.sha256("adults β received dha".encode()).hexdigest()
    protection.write_text(json.dumps({"source_only": True, "pmids": [], "normalized_text_hashes": [textkey]}), encoding="utf-8")
    with pytest.raises(ValueError, match="Actual selected"):
        prepare_clinical(source, allowed, output, protection)
    assert not output.exists()


def test_source_bytes_and_trusted_manifest_hash_are_actually_verified(tmp_path):
    source, allowed, output = fixture(tmp_path)
    with pytest.raises(ValueError, match="allowed-manifest SHA"):
        prepare_clinical(source, allowed, output, allowed_manifest_sha256="a" * 64)
    with (source / "rct-train.txt").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="source hash mismatch"):
        prepare_clinical(source, allowed, output)
    assert not output.exists()
