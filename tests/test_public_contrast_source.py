import sqlite3
import pytest
from nutrigenomics.public_contrast_source import indexed_contrast_source

def test_comparison_count_is_covering_and_view_values_rights_ties_unchanged():
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.executescript('''CREATE TABLE effect(id TEXT,series_accession TEXT,analysis_version TEXT,contrast TEXT,feature_type TEXT,feature_id TEXT,value REAL,metadata_json TEXT);
    CREATE INDEX public_effect_feature ON effect(feature_type,feature_id);
    CREATE INDEX public_effect_page ON effect(series_accession,analysis_version,contrast,feature_type,feature_id);
    CREATE VIEW study_effect AS SELECT id,series_accession,analysis_version,contrast,feature_type,feature_id,value,json_set(metadata_json,'$.export_allowed',json('true')) AS metadata_json,NULL AS source_file FROM effect;''')
    db.executemany('INSERT INTO effect VALUES(?,?,?,?,?,?,?,?)',[(f'id{i}','S','v',f'c{i%20}','gene','APOE',None if i%2 else 0.,'{"source":"original"}') for i in range(200)])
    source=indexed_contrast_source(db);assert 'INDEXED BY public_effect_page' in source
    where="series_accession=? AND analysis_version=? AND contrast=? AND feature_type IN ('gene','expression') AND feature_id IN (?,?)"
    for c in ['c0','c1','missing']:
        args=['S','v',c,'APOE','unknown']
        count='SELECT count(*) FROM '+source+' WHERE '+where
        plan=' '.join(str(tuple(r)) for r in db.execute('EXPLAIN QUERY PLAN '+count,args))
        assert 'COVERING INDEX public_effect_page' in plan
        assert db.execute(count,args).fetchone()[0]==db.execute('SELECT count(*) FROM study_effect WHERE '+where,args).fetchone()[0]
        for limit in [1,6,200]:
            suffix=' WHERE '+where+' ORDER BY feature_type,feature_id LIMIT ?'
            assert [tuple(r) for r in db.execute('SELECT * FROM '+source+suffix,[*args,limit])]==[tuple(r) for r in db.execute('SELECT * FROM study_effect'+suffix,[*args,limit])]
    db.close()

def test_unknown_index_layout_keeps_legacy_path():
    db=sqlite3.connect(':memory:');assert indexed_contrast_source(db)=='study_effect'
    db.executescript('CREATE TABLE effect(id,feature_id);CREATE INDEX public_effect_page ON effect(feature_id);')
    assert indexed_contrast_source(db)=='study_effect';db.close()
