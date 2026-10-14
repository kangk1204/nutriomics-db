import json
import sqlite3
from types import SimpleNamespace

from sqlalchemy import event
from test_bridge_api import session as session  # noqa: PLC0414 -- shared pytest fixture

from nutrigenomics.bridge import _effects
from nutrigenomics.explorer_performance import canonical, fingerprint, selector_key
from nutrigenomics.models import StudyEffect


def setup_index(session, tmp_path, monkeypatch):
    contrast = SimpleNamespace(series_accession="GSETEST", contrast="post-pre", analysis_version="v1")
    for f, value in (("B", 0.0), ("A", None), ("C", -0.25)):
        session.add(StudyEffect(series_accession="GSETEST", feature_type="gene_expression", feature_id=f,
                                contrast="post-pre", analysis_version="v1", effect_estimate=value, q_value=0.8))
    session.commit()
    core = session.get_bind().url.database
    projection = tmp_path / "index.sqlite"
    key = selector_key(vars(contrast))
    with sqlite3.connect(projection) as db:
        db.executescript("CREATE TABLE meta(key TEXT,value_json TEXT); CREATE TABLE perf_effect_selector(selector TEXT,total INTEGER); CREATE TABLE perf_effect_ref(selector TEXT,effect_id INTEGER,feature_type TEXT,feature_id TEXT,ordinal_all INTEGER);")
        db.executemany("INSERT INTO meta VALUES(?,?)", [("release_id", json.dumps("v1")), ("performance", canonical({"indexed_for_release": "v1", "core_fingerprint": json.loads(canonical(fingerprint(core)))}))])
        db.execute("INSERT INTO perf_effect_selector VALUES(?,3)", (key,))
        with sqlite3.connect(core) as source:
            rows = source.execute("SELECT id,feature_type,feature_id FROM study_effect ORDER BY feature_id").fetchall()
        db.executemany("INSERT INTO perf_effect_ref VALUES(?,?,?,?,?)", [(key, *r, i) for i, r in enumerate(rows)])
    monkeypatch.setenv("NUTRIGENOMICS_EXPLORER_DATABASE", str(projection))
    return contrast, projection


def test_index_skips_count_but_keeps_original_values_and_order(session, tmp_path, monkeypatch):
    contrast, _ = setup_index(session, tmp_path, monkeypatch)
    statements = []
    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(session.get_bind(), "before_cursor_execute", trace)
    try:
        rows, total = _effects(session, contrast, limit=2)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", trace)
    assert total == 3
    assert [(r["feature_id"], r["effect_estimate"]) for r in rows] == [("A", None), ("B", 0.0)]
    assert not any("count(" in s.lower() for s in statements)


def test_core_change_never_reuses_stale_count(session, tmp_path, monkeypatch):
    contrast, _ = setup_index(session, tmp_path, monkeypatch)
    session.add(StudyEffect(series_accession="GSETEST", feature_type="cpg", feature_id="cg1", contrast="post-pre", analysis_version="v1"))
    session.commit()
    assert _effects(session, contrast)[1] == 4


def test_reference_key_mismatch_falls_back(session, tmp_path, monkeypatch):
    contrast, projection = setup_index(session, tmp_path, monkeypatch)
    with sqlite3.connect(projection) as db:
        db.execute("UPDATE perf_effect_ref SET feature_id='WRONG'")
    assert [r["feature_id"] for r in _effects(session, contrast)[0]] == ["A", "B", "C"]
