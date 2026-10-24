"""Exercise the post-NER dictionary/candidate route without a legacy HOME."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"literature_service"))
from nutri_literature.evidence_v8.common import atomic, digest, filehash, home, read
from nutri_literature.evidence_v8.dictionary_mentions import asset_contract, extract


class FixtureKB:
    def __init__(self, directory):
        self.home = directory
        self.base = sqlite3.connect((directory/"aliases.sqlite3").resolve().as_uri()+"?mode=ro&immutable=1", uri=True)
        self.overlay = sqlite3.connect((directory/"knowledge/overlay.sqlite3").resolve().as_uri()+"?mode=ro&immutable=1", uri=True)

    def cell_species(self, text):
        return []

    def by_id(self, kind, identifier):
        for db in (self.overlay, self.base):
            r = db.execute("SELECT native_id,taxon,names FROM records WHERE kind=? AND native_id=?", (kind, identifier)).fetchone()
            if r:
                return {"native_id": r[0], "taxon": r[1], "names": json.loads(r[2])}
        return None

    def lexical(self, kind, surface, expansions=()):
        from nutri_literature.repair_v6_gpu_r2.knowledge import namekey
        found = set()
        for db in (self.base, self.overlay):
            for term in (surface, *expansions):
                found.update(r[0] for r in db.execute("SELECT r.native_id FROM records r JOIN aliases a ON a.rid=r.rid WHERE a.kind=? AND a.norm=?", (kind, namekey(term))))
        return [(identifier, 1, 1.) for identifier in sorted(found)]

    def close(self):
        self.base.close()
        self.overlay.close()


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from nutri_literature.repair_v6_gpu_r2.knowledge import namekey
    monkeypatch.setenv("EVIDENCE_V8_HOME", str(tmp_path))
    monkeypatch.setenv("REPAIR_GPU_HOME", str(tmp_path/"legacy-path-must-not-be-read"))
    (tmp_path/"knowledge").mkdir()
    records = [("gene", "1636", 9606, ["ACE"]), ("chemical", "D00001", None, ["caffeine"]),
               ("chemical", "D99999", None, ["hypertension"])]
    for path in (tmp_path/"aliases.sqlite3", tmp_path/"knowledge/overlay.sqlite3"):
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE records(rid INTEGER PRIMARY KEY,kind TEXT,native_id TEXT,taxon INTEGER,names TEXT);"
                         "CREATE TABLE aliases(norm TEXT,kind TEXT,rid INTEGER)")
        for kind, identifier, taxon, names in records:
            c = db.execute("INSERT INTO records(kind,native_id,taxon,names) VALUES(?,?,?,?)", (kind, identifier, taxon, json.dumps(names)))
            db.executemany("INSERT INTO aliases VALUES(?,?,?)", [(namekey(n), kind, c.lastrowid) for n in names])
        db.commit()
        db.close()
    atomic(tmp_path/"knowledge/manifest.json", {"base_sha256": filehash(tmp_path/"aliases.sqlite3"),
            "overlay_sha256": filehash(tmp_path/"knowledge/overlay.sqlite3")})
    atomic(tmp_path/"knowledge/chemical-categories.json", {"chemical_ids": ["D00001"],
            "source_sha256": {"desc2026.gz": "0"*64, "supp2026.gz": "1"*64}, "selected_using_evaluation_answers": False})
    paths = ("aliases.sqlite3", "knowledge/overlay.sqlite3", "knowledge/manifest.json", "knowledge/chemical-categories.json")
    atomic(tmp_path/"inputs/import.json", {"files": {p: filehash(tmp_path/p) for p in paths}})
    atomic(tmp_path/"retrieval/manifest.json", {"synthetic_cpu_integration_index": True})
    return tmp_path


def test_explicit_categories_use_v8_home_and_are_hash_bound(runtime):
    contract, categories = asset_contract()
    assert categories.allowed == frozenset({"D00001"})
    assert not contract["legacy_home_used"]
    assert contract["files"]["knowledge/chemical-categories.json"] == categories.sha256
    assert not (runtime/"legacy-path-must-not-be-read").exists()
    kb = FixtureKB(runtime)
    try:
        text = "Human ACE changed with caffeine in hypertension."
        mentions = extract(text, kb, categories)
        assert {m["text"] for m in mentions} == {"ACE", "caffeine"}
        assert all(text[m["start"]:m["end"]] == m["text"] for m in mentions)
        assert all(not m["dictionary_match_is_automatic_approval"] for m in mentions)
    finally:
        kb.close()


def test_missing_or_modified_category_fails_before_model_work(runtime):
    path = runtime/"knowledge/chemical-categories.json"
    atomic(path, {"chemical_ids": ["changed"]})
    with pytest.raises(ValueError, match="해시"):
        asset_contract()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        asset_contract()


def test_old_import_contract_without_category_hash_is_rejected(runtime):
    p = runtime/"inputs/import.json"
    v = read(p)
    del v["files"]["knowledge/chemical-categories.json"]
    atomic(p, v)
    with pytest.raises(ValueError, match="해시 계약"):
        asset_contract()


def test_post_ner_infer_executes_dictionary_and_real_candidate_assembly(runtime, monkeypatch):
    from nutri_literature.evidence_v8 import ner, retrieval
    from nutri_literature.repair_v6_gpu_r2 import dictionary_mentions as legacy
    monkeypatch.setattr(legacy, "extract", lambda *a, **k: pytest.fail("legacy HOME route invoked"))
    text = "Human ACE changed with caffeine in hypertension."
    lo = text.index("ACE")
    raw = {"kind": "gene", "start": lo, "end": lo+3, "text": "ACE", "source": "v8-crossfit-NER", "ner_score": .9}
    model = runtime/"fixture-trained-NER"
    model.mkdir()
    atomic(model/"training-provenance.json", {"pmids": ["1"], "synthetic_integration_fixture": True})
    called = []
    monkeypatch.setattr(ner, "fit", lambda docs: called.append("fit") or model)
    monkeypatch.setattr(ner, "predict_sources", lambda docs, path: called.append("predict") or {"predictions": {"2": [raw]}})
    real_enrich = retrieval.Retriever.enrich
    class CPUQuery:
        def __enter__(self):
            self.kb = FixtureKB(runtime)
            return self
        def query(self, kind, surface):
            return [(r[0], 1.) for r in self.kb.lexical(kind, surface)]
        def enrich(self, d, m):
            return real_enrich(self, d, m)
        def __exit__(self, *args):
            self.kb.close()
    monkeypatch.setattr(retrieval, "Retriever", CPUQuery)
    before = {p: filehash(runtime/p) for p in ("aliases.sqlite3", "knowledge/overlay.sqlite3")}
    rows = ner.infer([{"pmid": "1", "text": "independent training source"}], [{"pmid": "2", "text": text}])
    assert called == ["fit", "predict"]
    assert {r["mention"]["text"] for r in rows} == {"ACE", "caffeine"}
    assert all(r["menu"][0] is None and len(r["menu"]) <= 51 for r in rows)
    assert next(r for r in rows if r["mention"]["kind"] == "chemical")["menu"] == [None, "D00001"]
    assert all("gold_ids" not in r for r in rows)
    assert before == {p: filehash(runtime/p) for p in before}
    profile = read(next((runtime/"ner/profiles").glob("*.json")))
    assert profile["dictionary_assets"]["files"]["knowledge/chemical-categories.json"] == filehash(runtime/"knowledge/chemical-categories.json")


def test_invalid_dictionary_blocks_before_expensive_ner_fit(runtime, monkeypatch):
    from nutri_literature.evidence_v8 import ner
    monkeypatch.setattr(ner, "fit", lambda docs: pytest.fail("model fitting must not start"))
    (runtime/"knowledge/chemical-categories.json").unlink()
    with pytest.raises(FileNotFoundError):
        ner.infer([{"pmid": "1", "text": "training"}], [{"pmid": "2", "text": "independent test"}])


def test_dictionary_change_alters_model_job_contract_even_for_same_rows(runtime):
    from nutri_literature.evidence_v8.learning import model_contract
    atomic(runtime/"models.json", {"encoder": {"repository": "synthetic-unit-only", "revision": "0"*40}})
    before = model_contract("ner", [], [], {"synthetic_unit_only": True}, 20261023)
    p = runtime/"knowledge/chemical-categories.json"
    value = read(p)
    atomic(p, {**value, "chemical_ids": [*value["chemical_ids"], "D88888"]})
    imported = read(runtime/"inputs/import.json")
    imported["files"]["knowledge/chemical-categories.json"] = filehash(p)
    atomic(runtime/"inputs/import.json", imported)
    after = model_contract("ner", [], [], {"synthetic_unit_only": True}, 20261023)
    assert before != after
