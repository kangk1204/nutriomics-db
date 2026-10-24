"""Add only the isolated v5 worker to the existing user supervisor service."""

import argparse
import configparser
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import shlex


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main", type=Path, default=Path("/home/keunsoo/projects/nutriomics-literature"))
    parser.add_argument(
        "--development",
        type=Path,
        default=Path("/home/keunsoo/projects/nutriomics-literature-v5-development"),
    )
    args = parser.parse_args()
    main_root = args.main.resolve(strict=True)
    development = args.development.resolve(strict=True)
    python = development / ".venv/bin/python"
    models = development / "runtime/models"
    clinical = models / "clinical"
    runtime = main_root / "runtime"
    for required in (
        python,
        models / "gene-snapshot.json",
        clinical / "manifest.json",
        models / "learned-profiles/manifest.json",
    ):
        if not required.exists():
            raise SystemExit(f"Missing v5 dependency: {required}")
    fragment = f'''[program:source_extract_v5]
directory={development}
command={python} -m nutri_literature.cli_v5 worker --limit 10
environment=LITERATURE_HOME="{runtime}",LITERATURE_V5_MODEL_HOME="{models}",LITERATURE_V5_CLINICAL_HOME="{clinical}",LITERATURE_V5_LEARNED_HOME="{models}/learned-profiles",LITERATURE_V5_RELATION_CONFIGURATION="relation_logistic_0p1",LITERATURE_V5_RELATION_THRESHOLD="0.85",LITERATURE_NLP_LOCK="{runtime}/locks/nlp-cpu.lock",OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1",MKL_NUM_THREADS="1",NUMEXPR_NUM_THREADS="1",TOKENIZERS_PARALLELISM="false"
autostart=true
autorestart=true
startsecs=5
startretries=5
stopasgroup=true
killasgroup=true
stdout_logfile=%(ENV_LITERATURE_HOME)s/logs/worker-v5.log
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
    section = "program:source_extract_v5"
    if existing.has_section(section):
        if dict(existing[section]) != dict(expected[section]):
            raise SystemExit("Existing v5 supervisor program differs; preserve it and reconcile explicitly.")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = runtime / "backups" / f"supervisord-pre-v5-{stamp}.conf"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(original, encoding="utf-8")
        staged = target.with_suffix(".v5.tmp")
        staged.write_text(original.rstrip() + "\n\n" + fragment, encoding="utf-8")
        staged.replace(target)
        print(f"Preserved existing supervisor configuration: {backup}", flush=True)
    env = {**os.environ, "LITERATURE_HOME": str(runtime)}
    ctl = [str(main_root / ".venv/bin/supervisorctl"), "-c", str(target)]
    for operation in (["reread"], ["update", "source_extract_v5"], ["status", "source_extract_v5"]):
        subprocess.run(ctl + operation, env=env, cwd=main_root, check=True)
    # An independent observer preserves samples even when the NLP worker is busy.
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if current.returncode not in (0, 1):
        raise SystemExit("Cannot read existing user crontab")
    marker = "# BEGIN NUTRIOMICS V5 OBSERVER"
    ending = "# END NUTRIOMICS V5 OBSERVER"
    command = f"cd {shlex.quote(str(development))} && LITERATURE_HOME={shlex.quote(str(runtime))} {shlex.quote(str(python))} {shlex.quote(str(development / 'scripts/observe_v5.py'))}"
    row = f"*/5 * * * * /usr/bin/flock -n {shlex.quote(str(runtime / 'locks/observer-v5.lock'))} /bin/sh -c {shlex.quote(command)} >>{shlex.quote(str(runtime / 'logs/observer-v5.log'))} 2>&1"
    block = marker + "\n" + row + "\n" + ending
    if marker in current.stdout:
        start = current.stdout.index(marker)
        end = current.stdout.find(ending, start)
        if end < 0:
            raise SystemExit("Malformed existing v5 observer block; preserve it")
        value = current.stdout[:start] + block + current.stdout[end + len(ending) :]
    else:
        value = current.stdout.rstrip() + "\n\n" + block + "\n"
    if value != current.stdout:
        backup = (
            runtime
            / "backups"
            / ("crontab-pre-v5-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".txt")
        )
        backup.write_text(current.stdout, encoding="utf-8")
        subprocess.run(["crontab", "-"], input=value, text=True, check=True)


if __name__ == "__main__":
    main()
