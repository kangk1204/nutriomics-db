"""Harmless staging checks: auth/job lifecycle and a private online-backup restore drill."""
import json,os,secrets,shutil,sqlite3,sys,time,gzip,tempfile,hashlib
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from nutri_literature import auth,platform_control as control
from nutri_literature.settings import Settings
from nutri_literature.db import connect,digest,dumps


def main():
    settings=Settings.from_env();public='http://127.0.0.1:8064';internal='http://127.0.0.1:8063';checks={}
    def get(base,path):return requests.get(base+path,timeout=15)
    checks['public_has_no_admin']=get(public,'/api/v1/literature-admin/jobs').status_code==404
    checks['internal_requires_cookie']=get(internal,'/api/v1/explorer/literature/papers').status_code==401
    with connect(settings) as c:username=c.execute("SELECT username FROM platform_roles WHERE role='operator' ORDER BY username LIMIT 1").fetchone()[0]
    token=auth.create_session(settings,username)['access_token'];csrf=secrets.token_urlsafe(32);client=requests.Session();client.cookies.set('nutri_lit_session',token);client.cookies.set('nutri_lit_csrf',csrf)
    with connect(settings,durable=True) as c:c.execute('INSERT INTO platform_csrf VALUES(?,?)',(auth.token_hash(token),digest(csrf)))
    jid=None
    try:
        headers={'X-CSRF-Token':csrf,'Idempotency-Key':'platform-operational-check-'+secrets.token_hex(12)}
        checks['existing_account_cookie']=client.get(internal+'/api/v1/literature-admin/session',timeout=15).status_code==200
        payload={'kind':'collect','params':{}}
        checks['csrf_rejected']=client.post(internal+'/api/v1/literature-admin/jobs',json=payload,timeout=15).status_code==403
        checks['foreign_origin_rejected']=client.post(internal+'/api/v1/literature-admin/jobs',json=payload,headers={**headers,'Origin':'https://not-this-app.invalid'},timeout=15).status_code==403
        r=client.post(internal+'/api/v1/literature-admin/jobs',json=payload,headers=headers,timeout=15);r.raise_for_status();jid=r.json()['job_id']
        second=client.post(internal+'/api/v1/literature-admin/jobs',json=payload,headers=headers,timeout=15)
        checks['duplicate_job_same_ID']=second.json()['job_id']==jid
        r=client.post(internal+f'/api/v1/literature-admin/jobs/{jid}/cancel',headers=headers,timeout=15);checks['checkpoint_cancel']=r.status_code==200
        # Initial backfill owns collection; this test job cannot take that lock or run a second collector.
        r=client.post(internal+f'/api/v1/literature-admin/jobs/{jid}/resume',headers=headers,timeout=15);checks['checkpoint_resume']=r.status_code==200
    finally:
        if jid:client.post(internal+f'/api/v1/literature-admin/jobs/{jid}/cancel',headers={'X-CSRF-Token':csrf},timeout=15)
        auth.revoke_token(settings,token)
    checks['session_revoked']=client.get(internal+'/api/v1/literature-admin/session',timeout=15).status_code==401
    version=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');folder=settings.home/'verification'/('operations-'+version);folder.mkdir(parents=True,mode=0o700)
    import psutil
    if psutil.virtual_memory().available<32*1024**3:raise MemoryError('Restore drill requires the 24 GiB host reserve plus temporary storage')
    # Persist compressed backup on disk; private tmpfs work files avoid writing two uncompressed replicas to the shared analysis disk.
    archive=folder/'online-backup.sqlite3.gz';counts=[]
    with tempfile.TemporaryDirectory(prefix='nutriomics-platform-restore-',dir='/dev/shm') as temporary:
        backup=Path(temporary)/'backup.sqlite3';restored=Path(temporary)/'restored.sqlite3'
        with sqlite3.connect(settings.database) as source,sqlite3.connect(backup) as target:
            source.execute('BEGIN');source.execute('SELECT count(*) FROM papers').fetchone()
            source.backup(target,pages=1000,sleep=.01)
        with backup.open('rb') as raw,os.fdopen(os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stored:
            with gzip.GzipFile(fileobj=stored,mode='wb',compresslevel=1) as compressed:shutil.copyfileobj(raw,compressed,1024**2)
        with gzip.open(archive,'rb') as stored,restored.open('wb') as recovered:shutil.copyfileobj(stored,recovered,1024**2)
        def digest_file(p):
            with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
        checks['restored_bytes_match_backup']=digest_file(backup)==digest_file(restored)
        for path in (backup,restored):
            with sqlite3.connect(path) as c:
                checks['restore_integrity_'+path.stem]=c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                counts.append({table:c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('papers','sources','extractions','assertions','reviews','accounts')})
    checks['restored_counts_match_backup']=counts[0]==counts[1]
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'checks':checks,'passed':all(checks.values()),'backup_counts':counts[0],
            'passwords_changed':False,'expert_reviews_created':False,'production_replaced':False,'actual_7_day_observation_completed':False,
            'browser_personal_password_login':'not performed; cookie transport and roles tested separately','staging_job':jid}
    (folder/'receipt.json').write_text(dumps(result));print(dumps(result))
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
