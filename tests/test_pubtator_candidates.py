"""Synthetic BioC fixtures; these records are not presented as study measurements."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.collect_pubtator_candidates import (
    API_URL,
    MAX_PMIDS,
    collect,
    extract_candidates,
    fetch_response,
    read_studies,
    sha256,
)

PMID = "900000001"
TEXT = "α-test chemical was used in a randomized crossover study:8 mg for24 h; P=0.01."
CHEMICAL_START = TEXT.index("chemical")


def raw_fixture():
    return json.dumps({"PubTator3": [{
        "id": PMID,
        "passages": [{"infons": {"type": "abstract"}, "offset": 17, "text": TEXT,
                      "annotations": [{"id": "A1", "text": "chemical",
                                       "infons": {"type": "Chemical", "identifier": "SYNTHETIC:1"},
                                       "locations": [{"offset": 17+CHEMICAL_START, "length": 8}]}]}],
        "relations": [{"id": "R1", "infons": {"type": "positive_correlate"}}],
    }]}).encode()


def source(raw):
    return {"url": API_URL, "retrieved_at": "2026-10-01T00:00:00+00:00",
            "sha256": sha256(raw), "path": "private/synthetic.biocjson"}


def test_offsets_and_source_hash_preserved_without_validated_claims():
    raw = raw_fixture()
    package = extract_candidates(raw, {PMID: ["SYNTHETIC"]}, source(raw))
    doc = package["documents"][0]
    annotation = doc["entity_candidates"][0]
    assert annotation["span_check"] == "exact"
    assert annotation["locations"] == [{"offset": 17+CHEMICAL_START, "length": 8}]
    assert annotation["provenance"]["snapshot_sha256"] == sha256(raw)
    assert not annotation["normalization_verified"]
    assert all(value is None for value in doc["validated_interpretation"].values())
    assert not package["production_import_allowed"] and not doc["export_allowed"]
    assert 0 < len(doc["mention_candidates"]) < 200
    for mention in doc["mention_candidates"]:
        start = mention["offset"] - 17
        assert TEXT[start:start+mention["length"]] == mention["mention_text"]
        assert mention["validated_value"] is None
        assert not mention["assigned_to_intervention"]
        assert not mention["export_allowed"]
    relation = doc["relation_candidates"][0]
    assert relation["causal_or_health_claim"] is None
    assert relation["provenance"]["passage_index"] is None


def test_missing_ids_and_bad_offsets_are_not_filled_as_effects():
    data = json.loads(raw_fixture())
    data["PubTator3"][0]["passages"][0]["annotations"][0]["locations"][0]["offset"] = -1
    raw = json.dumps(data).encode()
    package = extract_candidates(raw, {PMID: ["SYNTHETIC"], "900000002": ["OTHER"]}, source(raw))
    assert package["missing_pmids"] == ["900000002"]
    assert package["documents"][0]["entity_candidates"][0]["span_check"] == "requires_review"
    assert package["documents"][0]["validated_interpretation"]["effect_estimate"] is None
    assert len(package["documents"]) == 1


def test_source_tampering_is_rejected():
    raw = raw_fixture()
    with pytest.raises(ValueError, match="hash"):
        extract_candidates(raw+b" ", {PMID: ["SYNTHETIC"]}, source(raw))


class FakeResponse:
    def __init__(self, raw):
        self.raw = raw
        self.url = API_URL + "?pmids=" + PMID + "&full_text=false"
        self.status_code = 200
        self.headers = {"Content-Type": "application/json", "Set-Cookie": "must-not-retain"}
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self.raw

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, raw):
        self.response = FakeResponse(raw)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def config_fixture(path: Path, studies=None):
    path.write_text(json.dumps({"version": "synthetic-v1", "studies": studies or [
        {"id": "SYNTHETIC", "pubmed_id": PMID},
    ]}), encoding="utf-8")


def test_fetch_is_bounded_abstract_only_and_does_not_store_sensitive_headers():
    client = FakeClient(raw_fixture())
    raw, meta = fetch_response(client, [PMID])
    assert raw == raw_fixture()
    assert client.calls[0][0] == API_URL
    assert client.calls[0][1]["params"] == {"pmids": PMID, "full_text": "false"}
    assert "Set-Cookie" not in meta["headers"]
    assert not meta["export_allowed"] and client.response.closed
    with pytest.raises(ValueError, match="byte limit"):
        fetch_response(FakeClient(raw), [PMID], max_bytes=4)


def test_frozen_reuse_checks_all_hashes_and_never_refetches(tmp_path):
    config, out = tmp_path/"studies.json", tmp_path/"collection"
    config_fixture(config)
    client = FakeClient(raw_fixture())
    result = collect(config, out, client=client)
    assert result["validated_scientific_assertions"] == 0
    assert result["production_records_written"] == 0
    assert result["entity_candidates"] == 1
    assert collect(config, out, client=client, reuse=True) == result
    assert len(client.calls) == 1
    with pytest.raises(FileExistsError, match="Frozen"):
        collect(config, out, client=client)
    candidate_file = out/"private/reviewer_candidates.json"
    candidate_file.write_bytes(candidate_file.read_bytes()+b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        collect(config, out, client=client, reuse=True)
    assert len(client.calls) == 1


def test_manifest_deduplicates_articles_and_rejects_unbounded_collection(tmp_path):
    config = tmp_path/"studies.json"
    config_fixture(config, [{"id": "FIRST", "pubmed_id": PMID}, {"id": "SECOND", "pubmed_id": PMID}])
    selected, _ = read_studies(config)
    assert selected == {PMID: ["FIRST", "SECOND"]}
    config_fixture(config, [{"id": str(i), "pubmed_id": str(900000001+i)} for i in range(MAX_PMIDS+1)])
    with pytest.raises(ValueError, match="bounded PMID"):
        read_studies(config)
