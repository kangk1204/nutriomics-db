"""Resume the full 2010 cohort; delayed/failed tasks prevent a completed receipt."""
import json,os,sys,time,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from contextlib import ExitStack
from nutri_literature.settings import Settings
from nutri_literature.db import connect,now,dumps
from nutri_literature.locking import lock
from nutri_literature.pipeline import process


def main():
    settings=Settings.from_env();os.nice(10);state=settings.home/'backfill-state.json'
    def save(**v):
        v.update(pid=os.getpid(),updated_at=time.time());temp=state.with_suffix('.part');temp.write_text(dumps(v));temp.replace(state)
    def inventory():
        with connect(settings) as c:
            return {'papers':c.execute('SELECT count(*) FROM papers').fetchone()[0],
                    'fulltext':c.execute("SELECT count(*) FROM papers WHERE oa_status='available'").fetchone()[0],
                    'queue':[dict(r) for r in c.execute('SELECT stage,status,count(*) AS count FROM queue GROUP BY stage,status')]}
    with ExitStack() as stack:
        for name in ('backfill','worker-extract','worker-oa'):stack.enter_context(lock(settings.home/'locks'/(name+'.lock')))
        # Exclusive stage locks prove no other worker owns these abandoned queue rows.
        with connect(settings,durable=True) as c:
            c.execute("UPDATE queue SET status='pending',due_at=?,error='recovered abandoned owner after failed backfill' WHERE status='running'",(now(),))
        try:
            for stage in ('extract','oa','extract'):
                while True:
                    value=process(settings,stage=stage,limit=20)
                    if stage=='oa':process(settings,stage='extract',limit=20)
                    save(running=True,stage='process-'+stage,last_batch=value,**inventory())
                    if not value['processed'] or value.get('throttled'):break
            counts=inventory();remaining=sum(v['count'] for v in counts['queue'] if v['status'] in ('pending','running','failed'))
            save(running=False,stage='waiting-retries' if remaining else 'completed',exit=0,remaining=remaining,**counts)
        except BaseException as exc:
            save(running=False,stage='failed',error_type=type(exc).__name__,error=str(exc),**inventory());traceback.print_exc();raise


if __name__=='__main__':main()
