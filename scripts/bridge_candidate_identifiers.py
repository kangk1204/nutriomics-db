"""Derive an identifier-linked release from a fully verified candidate projection."""
import argparse
import json
import os
import shutil
import sqlite3
from datetime import UTC,datetime
from pathlib import Path
from nutrigenomics.candidate_layers import add_exact_gene_bridges,validate_projection
from nutrigenomics.explorer_build import canonical,decode,sha256,read_only
from nutrigenomics.source_updates import atomic_json
from scripts.finalize_candidate_staging import finalize

def bridge(parent,root,hgnc,output,release_id,public=False):
    parent,root,hgnc,output=map(Path,(parent,root,hgnc,output))
    filename='nutriomics.sqlite3' if public else 'explorer.sqlite'
    original=json.loads((parent/'manifest.json').read_text(encoding='utf-8'))
    if sha256(parent/filename)!=original['database_sha256']:raise ValueError('Parent changed')
    staging=output.with_name('.'+output.name+'.building')
    if output.exists() or staging.exists():raise FileExistsError('Immutable derivative exists')
    staging.mkdir(parents=True)
    db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
    with read_only(parent/filename) as src:src.backup(db)
    db.execute('PRAGMA foreign_keys=ON')
    protected={'measurement','science_record','effect','effect_selector','contrast_selector','perf_effect_ref','perf_effect_selector','perf_effect_type','study_effect'}
    denied=[]
    def guard(action,table,column,database,trigger):
        if action in (sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,sqlite3.SQLITE_DROP_TABLE) and table in protected:
            denied.append([action,table]);return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.set_authorizer(guard)
    proof=add_exact_gene_bridges(db,hgnc)
    print(canonical({'phase':'exact_native_gene_id_links','proof':proof}),flush=True)
    # All existing perf ordinals are immutable. Only new identity edges enter adjacency.
    rows=[]
    for r in db.execute("SELECT id,subject,object,relation,tier FROM edge WHERE id LIKE 'HGNC-ID:%'"):
        if not db.execute('SELECT 1 FROM perf_adj WHERE edge_id=?',(r['id'],)).fetchone():
            rows.append((r['id'],r['subject'],r['object'],r['relation'],r['tier'],1,1,2,None,1))
    db.executemany('INSERT INTO perf_adj VALUES(?,?,?,?,?,?,?,?,?,?)',rows)
    db.execute("DELETE FROM extracted_only WHERE node_id IN (SELECT subject FROM edge WHERE id LIKE 'HGNC-ID:%' UNION SELECT object FROM edge WHERE id LIKE 'HGNC-ID:%')")
    db.execute('UPDATE node SET degree=(SELECT count(*) FROM edge WHERE subject=node.id)+(SELECT count(*) FROM edge WHERE object=node.id)')
    meta={r[0]:json.loads(r[1]) for r in db.execute('SELECT * FROM meta')}
    meta.update(release_id=release_id,created_at=datetime.now(UTC).isoformat(),
        derived_from={'release_id':original['release_id'],'database_sha256':original['database_sha256']},
        gene_identifier_bridge=proof,identifier_bridge_builder_sha256=sha256(Path(__file__)),
        identifier_bridge_rules_sha256=sha256(Path(__file__).parents[1]/'nutrigenomics/candidate_layers.py'))
    meta['candidate_layers']={**meta['candidate_layers'],
        'exact_gene_cross_references':meta['candidate_layers'].get('exact_gene_cross_references',0)+proof['added_exact_gene_identity_links'],
        'gene_identity_bridges':proof}
    meta.pop('files',None)
    for key in ('performance','graph_performance'):
        if key in meta:meta[key]={**meta[key],'indexed_for_release':release_id}
    if 'graph_performance' in meta:meta['graph_performance']['adjacency_rows']=db.execute('SELECT count(*) FROM perf_adj').fetchone()[0]
    meta['counts'].update(node=db.execute('SELECT count(*) FROM node').fetchone()[0],edge=db.execute('SELECT count(*) FROM edge').fetchone()[0],
        by_relation=dict(db.execute('SELECT relation,count(*) FROM edge GROUP BY relation')),
        by_tier=dict(db.execute('SELECT tier,count(*) FROM edge GROUP BY tier')),
        workbench_by_layer=dict(db.execute('SELECT layer,count(*) FROM workbench_evidence GROUP BY layer')))
    if public:meta['public_counts']={**meta.get('public_counts',{}),**meta['counts']}
    for k,v in meta.items():db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(k,canonical(v)))
    db.execute('ANALYZE');db.commit()
    accuracy=validate_projection(db,root,meta['candidate_layers'],public)
    accuracy.update(protected_table_writes_denied=denied,identifier_bridge=proof)
    atomic_json(staging/'source-accuracy.json',accuracy)
    if public:
        # Hard-link only immutable unchanged quantitative artifacts. Rewritten
        # graph tables are fresh files, never links to a parent's writable inode.
        rewrite={'edge','node','source','meta','workbench_evidence','candidate_document'}
        for p in parent.rglob('*'):
            if not p.is_file():continue
            rel=p.relative_to(parent)
            if rel.parts[0]=='effects' or (rel.parts[0]=='tables' and p.name.split('.')[0] not in rewrite):
                dest=staging/rel;dest.parent.mkdir(parents=True,exist_ok=True);os.link(p,dest)
            elif rel.parts[0]=='review' or (len(rel.parts)==1 and p.name not in {'manifest.json',filename,'source-accuracy.json','closed-integrity.json','coverage.json','candidate-layers.json'}):
                dest=staging/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
        from nutrigenomics.publication import _csv
        for table in sorted(rewrite):
            cursor=db.execute('SELECT * FROM '+table)
            _csv(staging/'tables'/(table+'.csv.gz'),[c[0] for c in cursor.description],cursor)
        metadata=json.loads((staging/'dataset-metadata.json').read_text(encoding='utf-8'))
        metadata.update(release_id=release_id,dataset_release_id=release_id,created_at=meta['created_at'],
            counts=meta['public_counts'],graph_counts=meta['counts'],gene_identifier_bridge=proof)
        atomic_json(staging/'dataset-metadata.json',metadata)
    print(canonical({'phase':'identifier_bridge_snapshot'}),flush=True)
    dest=sqlite3.connect(staging/filename);db.backup(dest,pages=8192);dest.close();db.close()
    return finalize(staging,output,public)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('parent','root','hgnc','output'):p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--release-id',required=True);p.add_argument('--public',action='store_true')
    a=p.parse_args();bridge(a.parent,a.root,a.hgnc,a.output,a.release_id,a.public)
