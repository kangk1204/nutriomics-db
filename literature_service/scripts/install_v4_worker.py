"""Add only the isolated v4 worker to the existing user supervisor service."""
import argparse
import configparser
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main", type=Path, default=Path("/home/keunsoo/projects/nutriomics-literature"))
    parser.add_argument("--development", type=Path, default=Path("/home/keunsoo/projects/nutriomics-literature-v4-development"))
    args = parser.parse_args()
    main_root = args.main.resolve(strict=True)
    development = args.development.resolve(strict=True)
    python = development / ".venv/bin/python"
    models = development / "runtime/models"
    clinical = models / "clinical-audited"
    runtime = main_root / "runtime"
    for required in (python, models / "gene-snapshot.json", clinical / "manifest.json"):
        if not required.exists():
            raise SystemExit(f"Missing v4 dependency: {required}")
    fragment = f'''[program:source_extract_v4]
directory={development}
command={python} -m nutri_literature.cli_v4 worker --limit 10
environment=LITERATURE_HOME="{runtime}",LITERATURE_V4_MODEL_HOME="{models}",LITERATURE_V4_CLINICAL_HOME="{clinical}",LITERATURE_NLP_LOCK="{runtime}/locks/nlp-cpu.lock",OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1",MKL_NUM_THREADS="1",NUMEXPR_NUM_THREADS="1",TOKENIZERS_PARALLELISM="false"
autostart=true
autorestart=true
startsecs=5
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=%(ENV_LITERATURE_HOME)s/logs/worker-v4.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
redirect_stderr=true
'''
    target = main_root / "scripts/supervisord.conf"
    original = target.read_text(encoding="utf-8")
    existing = configparser.ConfigParser(interpolation=None)
    existing.read_string(original)
    expected = configparser.ConfigParser(interpolation=None)
    expected.read_string(fragment)
    section = "program:source_extract_v4"
    if existing.has_section(section):
        if dict(existing[section]) != dict(expected[section]):
            raise SystemExit("Existing v4 supervisor program differs; preserve it and reconcile explicitly.")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = runtime / "backups" / f"supervisord-pre-v4-{stamp}.conf"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(original, encoding="utf-8")
        staged = target.with_suffix(".v4.tmp")
        staged.write_text(original.rstrip() + "\n\n" + fragment, encoding="utf-8")
        staged.replace(target)
        print(f"Preserved existing supervisor configuration: {backup}", flush=True)
    env = {**os.environ, "LITERATURE_HOME": str(runtime)}
    ctl = [str(main_root / ".venv/bin/supervisorctl"), "-c", str(target)]
    for operation in (["reread"], ["update", "source_extract_v4"], ["status", "source_extract_v4"]):
        subprocess.run(ctl + operation, env=env, cwd=main_root, check=True)


if __name__ == "__main__":
    main()
