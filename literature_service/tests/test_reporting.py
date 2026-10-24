import csv
import json
import sqlite3
from pathlib import Path

import pytest

from nutri_literature.db import connect, digest, dumps, initialize
from nutri_literature.reporting import backup, generate_report
from nutri_literature.settings import PROJECT, Settings
from nutri_literature.extraction import RULES_VERSION


@pytest.fixture
def settings(tmp_path):
    settings = Settings(tmp_path / "runtime", PROJECT / "config/topics.json")
    initialize(settings)
    text = "Vitamin D inhibits VDR."
    summary = {
        "method": "미확인",
        "results": [{"text": text, "start": 0, "end": len(text)}],
        "limitations": "미확인",
    }
    with connect(settings) as con:
        con.execute(
            "INSERT INTO papers(paper_id,pmid,title,abstract,created_at,updated_at,current_source_id) "
            "VALUES(1,'12345678','<script>unsafe</script>',?,'2026-10-01T00:00:00+00:00',"
            "'2026-10-01T00:00:00+00:00','source')",
            (text,),
        )
        con.execute("INSERT INTO paper_topics VALUES(1,'vitD','fixture')")
        con.execute(
            "INSERT INTO sources VALUES('source',1,'pubmed',?,?,?,?,'[]',?,NULL,?)",
            (
                digest(text),
                digest(text),
                "synthetic.xml",
                text,
                "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "2026-10-01T00:00:00+00:00",
            ),
        )
        con.execute(
            "INSERT INTO extractions VALUES('extract',1,'source',?,'fixture-engine','fixture-dictionary',?,?,1)",
            (
                digest(text),
                dumps({"summary": summary, "rules_version": RULES_VERSION}),
                "2026-10-01T00:00:00+00:00",
            ),
        )
        con.execute(
            "INSERT INTO assertions VALUES('a','extract',1,'fp','CHEBI:27300','inhibits','HGNC:12679',"
            "?,0,?,'fixture','approved',?)",
            (text, len(text), dumps({"explicit_relation": True})),
        )
        con.execute("INSERT INTO accounts VALUES('reviewer','unused',1,'2026-10-01T00:00:00+00:00')")
        con.execute(
            "INSERT INTO reviews VALUES('baseline-review','a','reviewer','approved','',NULL,'fp','2026-10-01T00:00:00+00:00')"
        )
    return settings


def _report(settings):
    return generate_report(settings, "2026-09-30T00:00:00+00:00", "2026-10-02T00:00:00+00:00")


def test_report_snapshot_and_files_are_immutable_after_review_or_source_change(settings):
    first = _report(settings)
    before = {kind: Path(path).read_bytes() for kind, path in first["paths"].items()}
    assert first["counts"]["new_papers"] == 1
    assert first["counts"]["approved_relations"] == 1
    assert b"<script>unsafe</script>" not in before["html"]
    with connect(settings) as con:
        con.execute("UPDATE papers SET current_source_id='changed',title='Revised title' WHERE paper_id=1")
    second = _report(settings)
    assert second["report_id"] != first["report_id"]
    assert second["counts"]["approved_relations"] == 0
    assert {kind: Path(path).read_bytes() for kind, path in first["paths"].items()} == before
    with connect(settings) as con:
        frozen = con.execute(
            "SELECT snapshot_json,snapshot_hash FROM reports WHERE report_id=?", (first["report_id"],)
        ).fetchone()
    assert digest(frozen["snapshot_json"]) == frozen["snapshot_hash"]
    assert json.loads(frozen["snapshot_json"])["quality"]["precision"] is None
    assert "needs_review" in Path(second["paths"]["html"]).read_text(encoding="utf-8")


def test_report_applies_review_correction_consistently_and_quotes_source(settings):
    with connect(settings) as con:
        con.execute(
            "INSERT INTO reviews VALUES('review','a','reviewer','corrected','',?,'fp',?)",
            (dumps({"predicate": "associated_with"}), "2026-10-01T01:00:00+00:00"),
        )
    report = _report(settings)
    assert "associated_with" in Path(report["paths"]["markdown"]).read_text(encoding="utf-8")
    with Path(report["paths"]["csv"]).open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[-1]["predicate"] == "associated_with"
    assert rows[-1]["quote"] == "Vitamin D inhibits VDR."
    assert rows[-1]["source_hash"] == digest("Vitamin D inhibits VDR.")


def test_online_backup_restores_wal_database_and_retains_only_30_days(settings):
    old = settings.backups / "literature-20000101T000000Z-old.sqlite3"
    old.write_bytes(b"old")
    old.with_suffix(".json").write_text("{}")
    unrelated = settings.backups / "unrelated.txt"
    unrelated.write_text("keep")
    receipt = backup(settings)
    assert receipt["integrity"] == "ok" and receipt["removed_old_backups"] == 1
    assert not old.exists() and unrelated.exists()
    restored = sqlite3.connect(receipt["path"])
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT pmid FROM papers").fetchone()[0] == "12345678"
    restored.close()
    assert digest(Path(receipt["path"]).read_bytes()) == receipt["sha256"]


def test_report_rejects_ambiguous_or_reversed_period(settings):
    with pytest.raises(ValueError, match="timezone"):
        generate_report(settings, "2026-10-01", "2026-10-02")
    with pytest.raises(ValueError, match="before"):
        generate_report(settings, "2026-10-02T00:00:00+00:00", "2026-10-01T00:00:00+00:00")


def test_corrupted_source_is_never_counted_as_approved(settings):
    with connect(settings) as con:
        con.execute("UPDATE sources SET text_content='corrupted source' WHERE source_id='source'")
    result = _report(settings)
    assert result["counts"]["approved_relations"] == 0
    assert "needs_review" in Path(result["paths"]["markdown"]).read_text(encoding="utf-8")


@pytest.mark.parametrize("change", ["old_rules", "missing_review", "mismatched_review"])
def test_report_review_and_rule_version_gates_match_the_map(settings, change):
    with connect(settings) as con:
        if change == "old_rules":
            con.execute(
                "UPDATE extractions SET result_json=?", (dumps({"rules_version": "old", "summary": {}}),)
            )
        elif change == "missing_review":
            con.execute("DELETE FROM reviews")
        else:
            con.execute("UPDATE reviews SET fingerprint='other'")
    assert _report(settings)["counts"]["approved_relations"] == 0
