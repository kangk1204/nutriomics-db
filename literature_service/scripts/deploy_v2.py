"""Deploy this tested source to the named server project with a recoverable snapshot.

Run from the staging checkout using the isolated v2 Python environment.
This script only manages the four literature processes in this project's supervisor.
"""
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from nutri_literature.db import digest, dumps, initialize, now, uid
from nutri_literature.settings import Settings

TARGET = Path('/home/keunsoo/projects/nutriomics-literature')
PROJECT = Path(__file__).resolve().parents[1]


def main():
    if TARGET.resolve() != TARGET or PROJECT.resolve() != Path('/home/keunsoo/projects/nutriomics-literature-v2-staging'):
        raise ValueError('This deployment script requires the explicit staging/production paths')
    runtime = TARGET/'runtime'
    env={**os.environ,'LITERATURE_HOME':str(runtime),'LITERATURE_CONFIG':str(TARGET/'config'/'topics.json')}
    identifier=uid()
    receipt_dir=runtime/'deployments'/identifier
    receipt_dir.mkdir(parents=True,exist_ok=False)
    old_python=TARGET/'.venv'/'bin'/'python'
    new_python=TARGET/'.venv-v2'/'bin'/'python'
    def run(command, cwd=TARGET):
        return subprocess.run([str(part) for part in command],cwd=cwd,env=env,check=True,capture_output=True,text=True).stdout.strip()
    supervisor=[TARGET/'.venv'/'bin'/'supervisorctl','-c',TARGET/'scripts'/'supervisord.conf']
    record={'id':identifier,'started_at':now(),'target':str(TARGET),'source_checkout':str(PROJECT)}
    record['backup']=json.loads(run([old_python,'-m','nutri_literature.cli','backup']))
    # Snapshot the current source directly. The old deployment's bundler predates
    # .venv-v2 and would mistakenly archive its installed model/environment.
    with tarfile.open(receipt_dir/'previous-source.tar.gz','w:gz') as archive:
        for root, directories, files in os.walk(TARGET):
            directories[:] = sorted(d for d in directories if not d.startswith('.venv') and
                d not in {'runtime','dist','.git','__pycache__','.pytest_cache','.ruff_cache'} and not d.endswith('.egg-info'))
            for name in sorted(files):
                if name != '.env':
                    source=Path(root)/name
                    archive.add(source,arcname=source.relative_to(TARGET).as_posix(),recursive=False)
    record['previous_source_sha256']=digest((receipt_dir/'previous-source.tar.gz').read_bytes())
    archive_path=PROJECT/'source.tar.gz'
    record['deployed_archive_sha256']=digest(archive_path.read_bytes())
    processes=['source_extract','source_oa','literature_api','literature_ui']
    maintenance=runtime/'maintenance.flag'
    with maintenance.open('x',encoding='utf-8') as marker:
        marker.write('deploy-v2:'+identifier)
    for program in processes:
        run([*supervisor,'stop',program])
    try:
        with tarfile.open(archive_path,'r:gz') as archive:
            for member in archive.getmembers():
                resolved=(TARGET/member.name).resolve()
                if TARGET not in resolved.parents or not member.isfile():
                    raise ValueError('Source archive contains a non-file/outside destination')
            archive.extractall(TARGET,filter='data')
        run([new_python,'-m','pip','install','--no-deps','-e',TARGET])
        # The original uv-built service environment has no pip module. Its
        # editable source already points to TARGET; no dependency mutation is needed.
        record['original_service_environment']='existing editable checkout; dependency versions preserved'
        run([new_python,'-m','nutri_literature.cli','init'])
        # Move only the already imported development artifacts and their audited
        # source reservations. No scientific predictions/test labels exist here.
        staged_public=PROJECT/'runtime'/'public_benchmarks'
        if staged_public.exists():
            shutil.copytree(staged_public,runtime/'public_benchmarks',dirs_exist_ok=True)
            index=runtime/'public_benchmarks'/'v2-measurement-index.json'
            if index.exists():
                value=index.read_text(encoding='utf-8').replace(str(PROJECT/'runtime'),str(runtime))
                index.write_text(value,encoding='utf-8')
            import sqlite3
            source_db=PROJECT/'runtime'/'data'/'literature.sqlite3'
            source=sqlite3.connect(source_db.resolve().as_uri()+'?mode=ro',uri=True)
            destination=sqlite3.connect(runtime/'data'/'literature.sqlite3')
            try:
                for row in source.execute("SELECT * FROM audit WHERE kind LIKE 'public_%'"):
                    destination.execute('INSERT OR IGNORE INTO audit VALUES(?,?,?,?,?)',row)
                destination.commit()
            finally:
                destination.close()
                source.close()
        initialize(Settings(runtime,TARGET/'config'/'topics.json'))
        record['supervisor_reread']=run([*supervisor,'reread'])
        record['supervisor_update']=run([*supervisor,'update'])
        for program in processes:
            result=subprocess.run([str(x) for x in [*supervisor,'start',program]],cwd=TARGET,env=env,capture_output=True,text=True)
            if result.returncode and 'already started' not in result.stdout:
                raise RuntimeError(result.stdout+result.stderr)
        record['status']=run([*supervisor,'status'])
        record['completed_at']=now()
        record['passed']=True
        maintenance.unlink()
    except Exception as error:
        record.update(passed=False,error=str(error),completed_at=now())
        (receipt_dir/'receipt.json').write_text(dumps(record),encoding='utf-8')
        raise
    (receipt_dir/'receipt.json').write_text(dumps(record),encoding='utf-8')
    print(dumps(record),flush=True)


if __name__=='__main__':
    main()
