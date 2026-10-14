"""Independent read-only SQL profile, including legacy code; no query parameters logged."""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path


def profile(source, performance_module, seed, output, seconds=10):
    import importlib.util
    sys.path.insert(0, str(Path(source).resolve()))
    __import__("nutrigenomics")  # Initialize the legacy package before loading its diagnostic helper.
    spec=importlib.util.spec_from_file_location("nutrigenomics.explorer_performance", performance_module)
    runtime=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=runtime
    spec.loader.exec_module(runtime)
    original=sqlite3.connect
    budget=runtime.Budget(seconds)
    token=runtime.CURRENT.set(budget)
    def connect(*args, **kwargs):
        kwargs["factory"]=runtime.ProfileConnection
        db=original(*args, **kwargs)
        db.set_progress_handler(lambda:int(budget.expired()),1000)
        return db
    sqlite3.connect=connect
    def io():
        p=Path('/proc/self/io')
        return {k:int(v) for k,v in (line.split(':') for line in p.read_text().splitlines())} if p.exists() else {}
    before=io();started=time.monotonic()
    result={"seed_id":seed,"timeout_s":seconds,"measurement":"independent process, original function; not HTTP or cold OS cache"}
    try:
        from nutrigenomics import explorer
        data=explorer.graph(seed_id=seed,domain="all",tier="all",depth=4,max_nodes=80,max_edges=200,include_extracted=False)
        if budget.expired():
            result["status"]="query_timeout"
        else:
            result.update(status="available",nodes=len(data["nodes"]),edges=len(data["edges"]),release_id=data["release_id"])
            Path(output).with_suffix('.response.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    except Exception as exc:  # noqa: BLE001 -- Diagnostic boundary must persist every query failure.
        result.update(status="query_timeout" if budget.expired() else "query_failed",error_type=type(exc).__name__)
    finally:
        sqlite3.connect=original
        runtime.CURRENT.reset(token)
    result.update(seconds=time.monotonic()-started,sql_seconds=budget.sql_execution_seconds,statements=budget.statements,io_delta={k:v-before.get(k,0) for k,v in io().items()})
    p=Path('/proc/pressure/io')
    result['io_pressure']=p.read_text() if p.exists() else None
    result['source_sha256']=hashlib.sha256((Path(source)/'nutrigenomics/explorer.py').read_bytes()).hexdigest()
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--performance-module',type=Path,required=True);p.add_argument('--seed',default='FDC:2259793');p.add_argument('--output',type=Path,required=True);p.add_argument('--seconds',type=float,default=10)
    a=p.parse_args();print(json.dumps(profile(a.source,a.performance_module,a.seed,a.output,a.seconds),ensure_ascii=False))
