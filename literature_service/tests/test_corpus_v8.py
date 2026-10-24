"""Synthetic corpus export contracts; no scientific accuracy claims."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from nutri_literature.corpus_v8 import export_corpus, text_key, textmining_license


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_database(path, language=True):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE papers(pmid TEXT PRIMARY KEY,pmcid TEXT,pub_date TEXT,retracted INTEGER,current_source_id TEXT" +
                (",language TEXT" if language else "") + ")")
    con.execute("CREATE TABLE sources(source_id TEXT PRIMARY KEY,kind TEXT,sha256 TEXT,text_hash TEXT,text_content TEXT,sections TEXT,license TEXT)")
    con.commit()
    con.close()


def add_document(path, pmid, text, license="CC BY 4.0", language="eng", pmcid=None, pub_date="2026-10-23", kind="fulltext", retracted=0):
    con = sqlite3.connect(path)
    fields = {r[1] for r in con.execute("PRAGMA table_info(papers)")}
    values = (str(pmid), pmcid, pub_date, retracted, "source-" + str(pmid))
    if "language" in fields:
        values += (language,)
    con.execute("INSERT INTO papers VALUES(" + ",".join("?" for _ in values) + ")", values)
    con.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?)", ("source-" + str(pmid), kind, sha("raw " + text), sha(text), text,
                json.dumps([{"label": "BODY", "start": 0, "end": len(text)}]), license))
    con.commit()
    con.close()


def protect(path, **updates):
    payload = {"source_only": True, "all_public_gold_excluded": True,
               "pmids": [], "pmcids": [], "normalized_text_hashes": [],
               "public_gold_sources": [{"name": "synthetic-public-corpus", "split": "all", "sha256": "a" * 64}]}
    payload.update(updates)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    database = tmp_path / "source.sqlite3"
    create_database(database)
    return database, tmp_path / "export", protect(tmp_path / "protection.json")


def tokenized(database, output, exclusion, **kwargs):
    return export_corpus(database, output, exclusion, tokenizer_name="synthetic-tokenizer", tokenizer_revision="a" * 40,
                         token_counter=lambda s: len(s.split()), **kwargs)


def read_records(output, filename="documents.jsonl"):
    return [json.loads(line) for line in (output / filename).read_text(encoding="utf-8").splitlines()]


def test_protected_pmid_pmcid_and_normalized_text_are_all_excluded(setup):
    database, output, exclusion = setup
    add_document(database, 1, "First document")
    add_document(database, 2, "Second document", pmcid="PMC22")
    add_document(database, 3, "ＦＯＯ   Text")
    add_document(database, 4, "Allowed study")
    protect(exclusion, pmids=["1"], pmcids=["PMC22"], normalized_text_hashes=[text_key("foo text")])
    result = tokenized(database, output, exclusion)
    assert result["training_ready"] and result["counts"]["excluded_protected_document"] == 3
    assert [r["pmid"] for r in read_records(output)] == ["4"]
    assert result["protection_sha256"] == hashlib.sha256(exclusion.read_bytes()).hexdigest()


@pytest.mark.parametrize("license,allowed", [(None, False), ("CC BY-NC 4.0", False), ("CC BY-ND 4.0", False),
    ("Creative Commons", False), ("CC0", True), ("CC BY-SA 4.0", True),
    ("https://creativecommons.org/licenses/by/4.0/", True)])
def test_explicit_license_only(license, allowed):
    assert bool(textmining_license(license)) is allowed


def test_license_unknown_pubmed_excluded_and_no_paper_license_inheritance(setup):
    database, output, exclusion = setup
    add_document(database, 1, "No source license", license=None, kind="pubmed")
    add_document(database, 2, "Licensed full text")
    con = sqlite3.connect(database)
    con.execute("ALTER TABLE papers ADD COLUMN license TEXT")
    con.execute("UPDATE papers SET license='CC BY 4.0'")
    con.commit()
    con.close()
    result = tokenized(database, output, exclusion)
    assert [r["pmid"] for r in read_records(output)] == ["2"]
    assert result["counts"]["excluded_license_unknown_or_restricted"] == 1


def test_normalized_duplicate_keeps_deterministic_newest_document(setup):
    database, output, exclusion = setup
    add_document(database, 1, "FOO  β", pub_date="2025-01-01")
    add_document(database, 2, "Ｆｏｏ β", pub_date="2026-10-23")
    result = tokenized(database, output, exclusion)
    assert result["counts"]["excluded_normalized_duplicate"] == 1
    record = read_records(output)[0]
    assert record["pmid"] == "2" and record["text"] == "Ｆｏｏ β"
    assert record["text_end"] == len(record["text"])
    assert result["deduplication"].endswith("no semantic near-duplicate claim")


def test_no_tokenizer_is_candidate_only_not_false_token_budget_completion(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Verified English source")
    result = export_corpus(database, output, exclusion)
    assert result["status"] == "needs_tokenization"
    assert result["actual_tokens"] is None and not result["token_budget_verified"]
    assert not result["training_ready"] and result["files"] == []
    assert read_records(output)[0]["withheld_reasons"] == ["needs_tokenization"]


def test_missing_language_is_not_fabricated_as_english(tmp_path):
    database = tmp_path / "no-language.sqlite3"
    create_database(database, language=False)
    add_document(database, 1, "This looks English but metadata is absent.")
    output = tmp_path / "result"
    result = tokenized(database, output, protect(tmp_path / "protect.json"))
    assert result["status"] == "needs_language_verification" and not result["training_ready"]
    assert read_records(output)[0]["language"] == "unknown"
    assert not read_records(output)[0]["language_metadata_verified"]


def test_non_english_retained_as_candidate_never_in_training(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Texte français", language="fre")
    add_document(database, 2, "English source", language="eng")
    result = tokenized(database, output, exclusion)
    assert len(read_records(output)) == 2
    assert [r["pmid"] for r in read_records(output, "training.jsonl")] == ["2"]
    assert result["actual_tokens"] == 2


def test_token_budget_stops_on_whole_document_without_text_rewriting(setup):
    database, output, exclusion = setup
    add_document(database, 1, "one two three", pub_date="2026-10-23")
    add_document(database, 2, "four five six", pub_date="2025-01-01")
    result = tokenized(database, output, exclusion, max_tokens=4)
    assert result["status"] == "ready" and result["budget_stopped"]
    assert result["actual_tokens"] == 3 and result["actual_tokens"] <= result["max_tokens"]
    assert len(read_records(output, "training.jsonl")) == 1
    assert read_records(output)[0]["text"] == "one two three"


def test_global_gold_audit_missing_blocks_training_but_preserves_candidate_export(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Licensed source")
    protect(exclusion, all_public_gold_excluded=False, public_gold_sources=[])
    result = tokenized(database, output, exclusion)
    assert result["status"] == "waiting_global_public_gold_audit"
    assert not result["all_public_gold_excluded"] and result["files"] == []
    assert len(read_records(output)) == 1


def test_does_not_open_label_source_paths_and_rejects_inline_labels(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Source text")
    protect(exclusion, public_gold_sources=[{"name": "synthetic", "split": "all", "sha256": "b" * 64,
                                          "path": "Z:/does-not-exist/protected-labels.json"}])
    assert tokenized(database, output, exclusion)["training_ready"]
    second = output.with_name("second")
    protect(exclusion, labels=[])
    with pytest.raises(ValueError, match="labels"):
        tokenized(database, second, exclusion)


def test_source_hash_mismatch_never_creates_complete_manifest(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Source text")
    con = sqlite3.connect(database)
    con.execute("UPDATE sources SET text_hash=?", ("c" * 64,))
    con.commit()
    con.close()
    with pytest.raises(RuntimeError, match="hash mismatch"):
        tokenized(database, output, exclusion)
    assert not output.exists()
    partial = list(output.parent.glob("export.incomplete-*"))
    assert len(partial) == 1 and (partial[0] / "failure.json").exists()
    assert not (partial[0] / "manifest.json").exists()


def test_no_overwrite_or_floating_tokenizer_revision(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Source text")
    with pytest.raises(ValueError, match="40-character"):
        export_corpus(database, output, exclusion, tokenizer_name="test", tokenizer_revision="main")
    tokenized(database, output, exclusion)
    original = (output / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        tokenized(database, output, exclusion)
    assert (output / "manifest.json").read_bytes() == original


def test_pinned_wal_snapshot_does_not_include_concurrent_new_source(setup):
    database, output, exclusion = setup
    add_document(database, 1, "Source one")
    written = []

    def count(text):
        if not written:
            add_document(database, 2, "Concurrent source")
            written.append(True)
        return len(text.split())
    result = export_corpus(database, output, exclusion, tokenizer_name="synthetic", tokenizer_revision="a" * 40, token_counter=count)
    assert [r["pmid"] for r in read_records(output)] == ["1"]
    assert result["pinned_sqlite_read_snapshot"]
    assert sqlite3.connect(database).execute("SELECT count(*) FROM papers").fetchone()[0] == 2


def archive(path, pmid, raw, expected_hash=None):
    raw_path = path.with_name("archive-" + str(pmid) + ".xml")
    raw_path.write_bytes(raw)
    con = sqlite3.connect(path)
    if "raw_path" not in {r[1] for r in con.execute("PRAGMA table_info(sources)")}:
        con.execute("ALTER TABLE sources ADD COLUMN raw_path TEXT")
    con.execute("UPDATE sources SET raw_path=?,sha256=? WHERE source_id=?", (str(raw_path),
                expected_hash or hashlib.sha256(raw).hexdigest(), "source-" + str(pmid)))
    con.commit()
    con.close()
    return raw_path


@pytest.mark.parametrize("kind,raw,tag", [
    ("pubmed", b"<PubmedArticle><MedlineCitation><PMID>1</PMID><Article><Language>eng</Language></Article></MedlineCitation></PubmedArticle>",
     "PubmedArticle/MedlineCitation/Article/Language"),
    ("fulltext", b'<article xml:lang="en"><body><p>Source.</p></body></article>', "/article/@xml:lang")])
def test_archived_hash_verified_official_language_tags_enable_training_without_db_language(tmp_path, kind, raw, tag):
    database = tmp_path / "source.sqlite3"
    create_database(database, language=False)
    add_document(database, 1, "Licensed original source", kind=kind)
    archive(database, 1, raw)
    output = tmp_path / "export"
    result = tokenized(database, output, protect(tmp_path / "protection.json"))
    assert result["training_ready"] and result["archived_language_counts"] == {"verified": 1}
    record = read_records(output)[0]
    assert record["language"] == "en" and record["language_metadata_verified"]
    assert record["language_evidence"][-1]["tag_path"] == tag
    assert record["language_evidence"][-1]["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(result["language_evidence_sha256"]) == 64


@pytest.mark.parametrize("raw,status", [
    (b"<PubmedArticle><MedlineCitation><PMID>1</PMID><Article><Language>eng</Language><Language>fre</Language></Article></MedlineCitation></PubmedArticle>", "verified"),
    (b"<article><body><p>English-looking text without metadata.</p></body></article>", "language_tag_absent"),
    (b"<article xml:lang='en'><body></article>", "invalid_xml")])
def test_archived_language_multilingual_absent_or_invalid_is_unknown(setup, raw, status):
    database, output, exclusion = setup
    add_document(database, 1, "This source looks English.", language=None)
    archive(database, 1, raw)
    result = tokenized(database, output, exclusion)
    assert not result["training_ready"]
    record = read_records(output)[0]
    assert record["language"] == "unknown" and not record["language_metadata_verified"]
    assert record["language_evidence"][-1]["status"] == status


def test_archived_hash_mismatch_fails_closed_even_if_paper_metadata_says_english(setup):
    database, output, exclusion = setup
    add_document(database, 1, "English source", language="eng")
    archive(database, 1, b'<article xml:lang="en"/>', expected_hash="a" * 64)
    result = tokenized(database, output, exclusion)
    assert not result["training_ready"] and result["archived_language_counts"] == {"hash_mismatch": 1}
    assert read_records(output)[0]["language"] == "unknown"


def test_raw_sha_language_cache_is_bounded_and_reuses_batch_xml(tmp_path):
    from nutri_literature.corpus_v8 import ArchivedLanguage
    raw = b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><Language>eng</Language></Article></MedlineCitation></PubmedArticle><PubmedArticle><MedlineCitation><PMID>2</PMID><Article><Language>fre</Language></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    path = tmp_path / "batch.xml"
    path.write_bytes(raw)
    cache = ArchivedLanguage(limit=1)
    assert cache.read(path, hashlib.sha256(raw).hexdigest(), "1")["codes"] == ["eng"]
    assert cache.read(path, hashlib.sha256(raw).hexdigest(), "2")["codes"] == ["fre"]
    assert len(cache.cache) == 1
    other = tmp_path / "other.xml"
    other.write_bytes(b'<article xml:lang="en"/>')
    assert cache.read(other, hashlib.sha256(other.read_bytes()).hexdigest(), "3")["codes"] == ["en"]
    assert len(cache.cache) == 1
