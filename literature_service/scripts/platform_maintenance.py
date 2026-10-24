"""Cron entry: existing KST schedule, durable queue drain, immutable refresh, actual observations."""
import json,os,subprocess,sys,time,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.settings import Settings
from nutri_literature.locking import lock
from nutri_literature.db import connect,now,dumps


def main():
    settings=Settings.from_env();base=settings.home.parent;source=Path(os.environ['NUTRIOMICS_PLATFORM_ROOT']);os.nice(10)
    with lock(settings.home/'locks/platform-maintenance.lock'):
        state=settings.home/'backfill-state.json';status=json.loads(state.read_text()) if state.exists() else {}
        if status.get('running'):
            import psutil
            try:alive=any('platform_backfill.py' in v or 'backfill_pipeline.py' in v for v in psutil.Process(status['pid']).cmdline())
            except (psutil.NoSuchProcess,psutil.AccessDenied,KeyError):alive=False
            if not alive:
                status.update(running=False,stage='owner_lost',error='Backfill owner exited; exclusive stage locks will guard recovery')
                temporary=state.with_suffix('.recovery-part');temporary.write_text(dumps(status));temporary.replace(state)
        if not status.get('running'):
            subprocess.run([sys.executable,'-B','-m','nutri_literature.cli','tick'],env=os.environ,check=True)
            subprocess.run([sys.executable,'-B',str(source/'literature_service/scripts/platform_backfill.py')],env=os.environ,check=True)
        with connect(settings) as c:
            observation={'observed_at':now(),'papers':c.execute('SELECT count(*) FROM papers').fetchone()[0],
                         'failed_queue':c.execute("SELECT count(*) FROM queue WHERE status='failed'").fetchone()[0],
                         'running_queue':c.execute("SELECT count(*) FROM queue WHERE status='running'").fetchone()[0],
                         'queued':c.execute("SELECT count(*) FROM queue WHERE status='pending'").fetchone()[0],
                         'artifact':hashlib.sha256((source/'source-manifest.json').read_bytes()).hexdigest(),
                         'algorithm_7_day_soak_started':False}
        with (settings.home/'platform-observations.jsonl').open('a') as f:f.write(dumps(observation)+'\n')
        from audit_platform_collection import audit
        audit(settings)
        code="from pathlib import Path;import json;p=Path('/home/keunsoo/projects/nutriomics-relation-v6-gpu-r2/runtime/relation-repair-v6-gpu-r2/job-state.json');v=json.loads(p.read_text());print(json.dumps({k:v.get(k) for k in ['engine','running','stage','updated','exit','error']}))"
        import shlex
        try:
            result=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','keunsoo@100.93.148.94','python3 -c '+shlex.quote(code)],capture_output=True,timeout=12)
            training=json.loads(result.stdout) if result.returncode==0 else {'connection':'unavailable'}
        except (subprocess.TimeoutExpired,json.JSONDecodeError):training={'connection':'unavailable'}
        training['observed_at']=now();target=settings.home/'gpu-training-state.json';temp=target.with_suffix('.part');temp.write_text(dumps(training));temp.replace(target)
        stamp=settings.home/'last-platform-refresh.json'
        if not stamp.exists() or time.time()-stamp.stat().st_mtime>=3600:
            subprocess.run([sys.executable,'-B',str(source/'scripts/refresh_platform_snapshots.py')],cwd=source,check=True)
            stamp.write_text(dumps({'refreshed_at':now()}))
            subprocess.run([str(base/'.venv/bin/supervisorctl'),'-c',str(settings.home/'supervisor/supervisord.conf'),'restart','literature-public','literature-internal'],check=True)


if __name__=='__main__':
    try:main()
    except BlockingIOError:print('Another platform owner is active; this cron tick was skipped.')
