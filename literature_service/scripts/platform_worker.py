"""Run one durable management worker; stale owners recover to waiting, never success."""
import argparse,json,os,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nutri_literature.settings import Settings
from nutri_literature.db import connect,now
from nutri_literature.locking import lock
from nutri_literature import platform_control as control


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--once',action='store_true');parser.add_argument('--grant');parser.add_argument('--role',choices=['reviewer','operator'],default='reviewer');args=parser.parse_args()
    settings=Settings.from_env();control.initialize(settings)
    if args.grant:control.grant(settings,args.grant,args.role);print('Role updated');return
    root=Path(os.environ.get('NUTRIOMICS_PLATFORM_ROOT',Path(__file__).resolve().parents[2]))
    try:os.nice(10)
    except (AttributeError,PermissionError):pass
    with lock(settings.home/'locks/platform-worker.lock'):
        # Exclusive process lock proves the previous worker has released ownership.
        with connect(settings,durable=True) as con:
            con.execute("UPDATE platform_jobs SET status='waiting',error='worker restarted; checkpoint preserved' WHERE status IN ('running','cancel_requested')")
        while True:
            control.run_one(settings,root)
            if args.once:break
            time.sleep(5)


if __name__=='__main__':main()
