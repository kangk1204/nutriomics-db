import sqlite3,json
import pytest
from nutrigenomics.public_gene_previews import batch_gene_previews
from nutrigenomics.explorer_scientific import _cached_effects,_preview_key
from nutrigenomics.explorer_performance import read_connection,fingerprint,QueryUnavailable

@pytest.fixture
def fixture(tmp_path):
    path=tmp_path/'public.sqlite3';db=sqlite3.connect(path)
    db.executescript('''CREATE TABLE effect(id TEXT,series_accession TEXT,contrast TEXT,analysis_version TEXT,feature_type TEXT,feature_id TEXT,effect_estimate REAL,standard_error REAL,p_value REAL,q_value REAL,metadata_json TEXT);
    CREATE INDEX public_effect_feature ON effect(feature_type,feature_id);
    CREATE INDEX public_effect_page ON effect(series_accession,analysis_version,contrast,feature_type,feature_id);
    CREATE VIEW study_effect AS SELECT id,series_accession,contrast,analysis_version,feature_type,feature_id,effect_estimate,standard_error,p_value,q_value,json_set(metadata_json,'$.export_allowed',json('true')) AS metadata_json,NULL AS source_file FROM effect;
    CREATE TABLE effect_selector(series_accession,contrast,analysis_version,total);
    CREATE TABLE meta(key,value_json);''')
    rows=[('z','S','c1','v','gene','APOE',0.,None,.5,.9,'{"source":"a"}'),('a','S','c1','v','gene','APOE',-.2,.1,.2,.4,'{"source":"b"}'),('other','S','c1','v','cpg','APOE',.1,.02,.1,.2,'{}'),('third','S','c2','v','gene','APOE',.2,None,.3,.7,'{}')]
    db.executemany('INSERT INTO effect VALUES(?,?,?,?,?,?,?,?,?,?,?)',rows)
    db.executemany('INSERT INTO effect_selector VALUES(?,?,?,?)',[('S','c1','v',3),('S','c2','v',1),('S','confirmed_no_gene','v',1)])
    db.commit();db.close()
    contrasts=[dict(series_accession='S',contrast=c,analysis_version='v') for c in ['c1','c2','confirmed_no_gene','withheld']]
    gene=dict(id='HGNCsymbol:APOE',kind='gene',label='APOE')
    return path,contrasts,gene

def test_every_preview_field_count_null_permission_and_tie_matches_scalar(fixture):
    path,contrasts,gene=fixture
    with read_connection(path) as db:
        batch=batch_gene_previews(path,contrasts,gene,db,limit=1)
        for c in contrasts:
            key=_preview_key(fingerprint(path),fingerprint(path),c,gene,1)
            assert batch[key]==_cached_effects(path,c,gene,1,db)
        assert batch[_preview_key(fingerprint(path),fingerprint(path),contrasts[0],gene,1)][0][0]['id']=='z'
        assert batch[_preview_key(fingerprint(path),fingerprint(path),contrasts[-1],gene,1)][1] is None

def test_unknown_layout_falls_back_instead_of_synthesizing_zero(tmp_path):
    path=tmp_path/'legacy.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE fact(x)')
    db.close()
    with read_connection(path) as db:
        assert batch_gene_previews(path,[dict(series_accession='S',contrast='c',analysis_version='v')],dict(id='HGNCsymbol:APOE',label='APOE'),db) is None

def test_changed_source_fails_before_any_new_success_is_cached(fixture,monkeypatch):
    path,contrasts,gene=fixture
    import nutrigenomics.explorer_scientific as science
    original=science._format_effect;changed=[]
    before=fingerprint(path);key=_preview_key(before,before,contrasts[0],gene,6)
    def format_row(row,node):
        if not changed:
            with path.open('ab') as f:f.write(b'changed')
            changed.append(True)
        return original(row,node)
    monkeypatch.setattr(science,'_format_effect',format_row)
    with read_connection(path) as db:
        with pytest.raises(QueryUnavailable,match='source_changed'):batch_gene_previews(path,contrasts,gene,db)
    from nutrigenomics.explorer_performance import CACHE
    assert CACHE.get(key) is None
