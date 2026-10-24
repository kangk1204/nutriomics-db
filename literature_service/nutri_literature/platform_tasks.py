"""Checkpoint boundaries for management jobs; never runs inside a web request."""
import argparse,json,os,time
from pathlib import Path
from .settings import Settings
from .db import connect,now,dumps
from .locking import lock


def cancelled(settings,jid):
    with connect(settings) as con:
        row=con.execute('SELECT cancel_requested FROM platform_jobs WHERE job_id=?',(jid,)).fetchone()
    return not row or bool(row[0])


def drain(settings,jid):
    from .pipeline import process
    batches=[]
    for stage in ('extract','oa','extract'):
        with lock(settings.home/'locks'/('worker-'+stage+'.lock')):
            while not cancelled(settings,jid):
                value=process(settings,stage=stage,limit=20);batches.append(value)
                if not value['processed'] or value.get('throttled'):break
    with connect(settings) as con:
        unfinished=con.execute("SELECT count(*) FROM queue WHERE status IN ('pending','running','failed')").fetchone()[0]
    return {'status':'cancelled' if cancelled(settings,jid) else 'waiting' if unfinished else 'completed',
            'remaining_queue':unfinished,'batches':len(batches),'last_batch':batches[-1] if batches else None}


def execute(settings,jid):
    with connect(settings) as con:row=dict(con.execute('SELECT * FROM platform_jobs WHERE job_id=?',(jid,)).fetchone())
    kind=row['kind'];params=json.loads(row['params_json'])
    if kind=='collect':
        from .collection import collect
        with connect(settings) as con:topics=[r[0] for r in con.execute('SELECT topic_id FROM topics WHERE enabled=1')]
        for topic in ([params['topic']] if params.get('topic') else topics):
            if cancelled(settings,jid):return {'status':'cancelled','checkpoint':'topic metadata committed'}
            with lock(settings.home/'locks/collection.lock'):collect(settings,False,topic)
        return drain(settings,jid)
    if kind=='reprocess':
        from .pipeline import reprocess
        if params.get('engine','heuristic-v1')!='heuristic-v1':raise ValueError('r2 full-corpus contract not ready')
        reprocess(settings,'heuristic-v1');return drain(settings,jid)
    if kind=='build-report':
        from .reporting import generate_report
        return {'status':'completed','report':generate_report(settings)}
    raise ValueError('Unsupported CPU checkpoint task')


def main():
    p=argparse.ArgumentParser();p.add_argument('--job',required=True);args=p.parse_args();settings=Settings.from_env()
    try:os.nice(10)
    except (AttributeError,PermissionError):pass
    result=execute(settings,args.job)
    target=settings.home/'job-results'/(args.job+'.json');target.parent.mkdir(exist_ok=True)
    temporary=target.with_suffix('.part');temporary.write_text(dumps(result));temporary.replace(target)
    print(dumps(result),flush=True)


if __name__=='__main__':main()
