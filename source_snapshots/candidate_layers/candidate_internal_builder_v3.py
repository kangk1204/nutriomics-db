"""Build a fresh candidate-layer derivative while preserving every existing row."""
import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path
from datetime import UTC,datetime
from nutrigenomics.candidate_layers import integrate,validate_projection
from nutrigenomics.explorer_build import canonical,sha256,read_only,verify_closed_sqlite
from nutrigenomics.explorer_performance_build import add_graph_read_indexes
from nutrigenomics.source_updates import atomic_json

def build(parent,root,hgnc,output,release_id,public=False):
    parent,root,hgnc,output=map(Path,(parent,root,hgnc,output))
    manifest=json.loads((parent/'manifest.json').read_text(encoding='utf-8'))
    filename='nutriomics.sqlite3' if public else 'explorer.sqlite'
    if sha256(parent/filename)!=manifest['database_sha256']:raise ValueError('Parent hash changed')
    staging=output.with_name('.'+output.name+'.building')
    if output.exists() or staging.exists():raise FileExistsError('Immutable derivative already exists')
    staging.mkdir(parents=True)
    print(canonical({'phase':'copy_parent_to_bounded_memory','public':public}),flush=True)
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    db.execute('PRAGMA cache_size=-1048576')
    with read_only(parent/filename) as src:src.backup(db)
    db.execute('PRAGMA foreign_keys=ON')
    protected={'measurement','science_record','effect','effect_selector','contrast_selector',
        'study_effect','perf_effect_ref','perf_effect_selector','perf_effect_type'}
    attempted=[]
    def guard(action,table,column,database,trigger):
        if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                      sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE) and table in protected:
            attempted.append([action,table]);return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(guard)
    before={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('node','edge','measurement','science_record')}
    summary=integrate(db,root,hgnc,public)
    accuracy=validate_projection(db,root,summary,public)
    accuracy['protected_table_writes_denied']=attempted
    accuracy['protected_tables']=sorted(protected)
    atomic_json(staging/'source-accuracy.json',accuracy)
    print(canonical({'phase':'refresh_graph_only_indexes','summary':summary}),flush=True)
    # Effect reference ordinals and five-part keys are retained byte-for-byte in
    # their original tables. Rebuild only stale graph/search/exposure indexes.
    db.execute('DROP TABLE IF EXISTS perf_search')
    for name in ('perf_search_content','perf_adj','perf_exposure_match','node_kind'):
        db.execute('DROP TABLE IF EXISTS '+name)
    for name in ('perf_edge_tier','perf_node_label','perf_node_degree'):
        db.execute('DROP INDEX IF EXISTS '+name)
    db.execute('CREATE TABLE node_kind(id TEXT PRIMARY KEY,kind TEXT NOT NULL)')
    db.execute('INSERT INTO node_kind SELECT id,kind FROM node')
    db.execute('UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)+(SELECT count(*) FROM edge WHERE object=node.id)')
    db.execute('CREATE TABLE IF NOT EXISTS extracted_only(node_id TEXT PRIMARY KEY REFERENCES node(id))')
    db.execute('DELETE FROM extracted_only')
    db.execute("""INSERT INTO extracted_only WITH candidates(id) AS MATERIALIZED
        (SELECT subject FROM edge WHERE tier='extracted' UNION SELECT object FROM edge WHERE tier='extracted')
        SELECT id FROM candidates c WHERE NOT EXISTS
        (SELECT 1 FROM edge e WHERE (e.subject=c.id OR e.object=c.id) AND e.tier!='extracted')""")
    # Avoid the N(nodes)*N(exposures) comparison for regions that can never be a food exposure.
    indexes=add_graph_read_indexes(db,release_id)
    counts={t:db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in before}
    counts.update(by_kind=dict(db.execute('SELECT kind,count(*) FROM node GROUP BY kind')),
        by_tier=dict(db.execute('SELECT tier,count(*) FROM edge GROUP BY tier')),
        by_relation=dict(db.execute('SELECT relation,count(*) FROM edge GROUP BY relation')),
        workbench_by_layer=dict(db.execute('SELECT layer,count(*) FROM workbench_evidence GROUP BY layer')))
    if counts['measurement']!=before['measurement'] or counts['science_record']!=before['science_record']:
        raise ValueError('Protected scientific tables changed')
    current={**manifest,'release_id':release_id,'created_at':datetime.now(UTC).isoformat(),
        'derived_from':{'release_id':manifest['release_id'],'database_sha256':manifest['database_sha256']},
        'counts':{**manifest.get('counts',{}),**counts},'candidate_layers':summary,'graph_performance':indexes,
        'builder_code_sha256':sha256(Path(__file__)),'candidate_builder_sha256':sha256(Path(__file__).parents[1]/'nutrigenomics/candidate_layers.py')}
    if manifest.get('performance'):
        current['performance']={**manifest['performance'],'indexed_for_release':release_id,
            'source_release':manifest['release_id'],'effect_reference_reuse':'Unchanged original selector/reference tables and source core fingerprint'}
    current.pop('database_sha256',None)
    for k,v in current.items():db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,canonical(v)))
    db.commit()
    print(canonical({'phase':'integrity_and_sequential_snapshot','counts':counts}),flush=True)
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():
        raise ValueError('Extension integrity failure')
    if db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]>32_000_000_000:
        raise ValueError('Memory workspace exceeds 32GB limit')
    with sqlite3.connect(staging/filename) as dest:db.backup(dest,pages=2048)
    db.close()
    verify_closed_sqlite(staging/filename)
    current['database_sha256']=sha256(staging/filename)
    # Public attachments retain their original rights-filtered numeric payloads.
    # Old graph CSVs cannot be labelled as the new graph. Keep as explicit parent artifacts.
    if public:
        for name in ('data','schema.sql','README.md','LICENSE-DATA.md','dataset-metadata.json'):
            p=parent/name
            if p.is_dir():shutil.copytree(p,staging/name)
            elif p.is_file():shutil.copyfile(p,staging/name)
        metadata=json.loads((staging/'dataset-metadata.json').read_text(encoding='utf-8'))
        metadata.update(release_id=release_id,status='candidate_pending_expert_review',
            graph_counts=counts,candidate_layers=summary,derived_from_release=manifest['release_id'])
        atomic_json(staging/'dataset-metadata.json',metadata)
        atomic_json(staging/'graph-attachment-scope.json',{'graph_source':'nutriomics.sqlite3',
            'parent_attachment_release':manifest['release_id'],'parent_csv_scope':'unchanged original portable tables',
            'candidate_graph_export':'Use versioned API HTML/CSV/JSON export; bulk candidate CSV generated separately'})
    atomic_json(staging/'manifest.json',current)
    atomic_json(staging/'coverage.json',counts)
    atomic_json(staging/'candidate-layers.json',summary)
    os.replace(staging,output)
    return current

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('parent','root','hgnc','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--release-id',required=True);p.add_argument('--public',action='store_true')
    a=p.parse_args();print(canonical(build(a.parent,a.root,a.hgnc,a.output,a.release_id,a.public)))
