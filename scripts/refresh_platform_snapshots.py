"""Atomically build rights-separated sidecar releases; never modify the parent science DB."""
import json,os,sys,time,sqlite3
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutrigenomics.literature_snapshot import build_snapshot,canonical,filehash


def expansion_input(home):
    """Pin one immutable search inventory and its latest durable ingest receipt."""
    state_path=home/'v8-expansion/state.json'
    if not state_path.exists():return None
    state=json.loads(state_path.read_text(encoding='utf8'))
    path=Path(state.get('manifest',''))
    allowed=(home/'v8-expansion').resolve()
    if not path.is_absolute() or not path.resolve().is_relative_to(allowed):
        raise ValueError('Expansion manifest escaped its owned runtime')
    if not path.exists():return None  # Search enumeration has not completed.
    value=json.loads(path.read_text(encoding='utf8'))
    value['status']='running' if state.get('running') else state.get('stage','waiting')
    if value['status'] not in {'running','waiting','completed','failed'}:value['status']='waiting'
    value['observed_at']=state.get('updated_at',value.get('observed_at'))
    value['metadata_complete']=state.get('metadata_complete') is True
    value['missing_memberships']=state.get('missing_memberships')
    return value


def refresh(home):
    out=home/'platform-snapshots'
    release=os.environ.get('NUTRIOMICS_PUBLIC_RELEASE');science=json.loads((Path(release)/'dataset-metadata.json').read_text())['release_id'] if release else None
    version='literature-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');manifests={}
    baseline=home/'baseline-reference.json'
    # Load external receipts once for both views, just like the pinned WAL read below.
    expansion=expansion_input(home)
    diagnostics_path=home/'model-evaluation-v8/diagnostics.json'
    diagnostics=json.loads(diagnostics_path.read_text(encoding='utf8')) if diagnostics_path.exists() else None
    v8_evaluation=home/'model-evaluation-v8/evaluation.json'
    with sqlite3.connect((home/'data/literature.sqlite3').as_uri()+'?mode=ro',uri=True) as pinned:
        pinned.execute('BEGIN');pinned.execute('SELECT count(*) FROM papers').fetchone()
        for scope in ('public','internal'):
            target=out/version/scope
            evaluation=home/'model-evaluation/evaluation.json'
            manifests[scope]=build_snapshot(home/'data/literature.sqlite3',target,release_id=version,public=scope=='public',baseline_reference=baseline if baseline.exists() else None,science_release_id=science,evaluation_path=evaluation if evaluation.exists() else None,parent_database=Path(release)/'nutriomics.sqlite3' if release else None,_connection=pinned,expansion_manifest=expansion,diagnostics_manifest=diagnostics,v8_evaluation_path=v8_evaluation if v8_evaluation.exists() else None)
    if manifests['public']['source_transaction_sha256']!=manifests['internal']['source_transaction_sha256']:raise ValueError('Public/internal transaction identity differs')
    # A new composed release references two immutable inputs. No food/omics rows are rewritten.
    receipt={'release_id':version,'science_release_id':science,'parent_science_release_unchanged':True,
             'literature_snapshots':{k:{'relative_path':str((out/version/k).relative_to(out)),'manifest_sha256':filehash(out/version/k/'manifest.json')} for k in manifests}}
    (out/version/'composition.json').write_text(canonical(receipt))
    for scope in ('public','internal'):
        pointer=out/(scope+'-current');temporary=out/(scope+'-next-'+str(os.getpid()))
        temporary.symlink_to(out/version/scope,target_is_directory=True);temporary.replace(pointer)
    print(canonical({'release_id':version,'counts':manifests['public']['counts'],'composition':receipt}))


def main():
    home=Path(os.environ['LITERATURE_HOME']).resolve()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'literature_service'))
    from nutri_literature.locking import lock
    with lock(home/'locks/platform-snapshot-build.lock'):refresh(home)


if __name__=='__main__':main()
