"""Detached owned worker; exact stage identities and append-only logs."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run():
    os.chdir(ROOT)
    home = ROOT / 'runtime'
    for key in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
        os.environ[key] = '1'
    os.environ['LITERATURE_PROCESSING_LOCK_HOME'] = '/home/keunsoo/projects/nutriomics-literature-v6-development/runtime/locks'
    os.environ['LITERATURE_NLP_LOCK'] = os.environ['LITERATURE_PROCESSING_LOCK_HOME'] + '/nlp-cpu.lock'
    state = home / 'v7/baseline-state.json'
    data_root = home / 'v6/training-data/audited-complete-v6-e00e690ae91d2648'
    stages = [('prepare', ['prepare-separated', '--data', str(data_root / 'relation-train.json'), '--audit', str(data_root / 'audit.json'),
              '--cache', str(home / 'v6/refinement-r4/automatic-features'), '--out', str(home / 'v7/separated-features')]),
              ('fit', ['train-baseline', '--features', str(home / 'v7/separated-features'), '--out', str(home / 'v7/separated-head')])]
    def save(value):
        temp = state.with_suffix('.tmp')
        temp.write_text(json.dumps(value, sort_keys=True))
        temp.replace(state)
    for stage, args in stages:
        save({'stage': stage, 'status': 'running', 'pid': os.getpid(), 'at': time.time(), 'scientific_targets_met': False})
        completed = subprocess.run([sys.executable, '-m', 'nutri_literature.cli_v7', '--home', str(home), *args], cwd=ROOT)
        if completed.returncode:
            save({'stage': stage, 'status': 'failed', 'returncode': completed.returncode, 'at': time.time(), 'scientific_targets_met': False})
            return completed.returncode
    save({'stage': 'baseline', 'status': 'complete', 'at': time.time(), 'scientific_targets_met': False,
          'next': 'development comparison and evidence-specific features'})
    return 0


if __name__ == '__main__':
    if '--worker' in sys.argv:
        raise SystemExit(run())
    directory = ROOT / 'runtime/v7'
    directory.mkdir(parents=True, exist_ok=True)
    state = directory / 'baseline-state.json'
    if state.exists():
        old = json.loads(state.read_bytes())
        if old.get('status') == 'running':
            import psutil
            if psutil.pid_exists(old['pid']) and str(ROOT) in ' '.join(psutil.Process(old['pid']).cmdline()):
                raise SystemExit('Owned baseline already running')
    log = (directory / 'baseline.log').open('ab')
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker'], cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print(json.dumps({'pid': proc.pid, 'log': str(directory / 'baseline.log')}))
