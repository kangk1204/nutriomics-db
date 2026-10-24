"""Install one marked cron block, preserving and backing up all other entries."""

import argparse
import os
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

BEGIN = "# BEGIN NUTRIOMICS LITERATURE SERVICE"
END = "# END NUTRIOMICS LITERATURE SERVICE"
PROJECT = Path("/home/keunsoo/projects/nutriomics-literature")


def replace_block(existing, block):
    lines, kept, inside = existing.splitlines(), [], False
    for line in lines:
        if line == BEGIN:
            if inside:
                raise ValueError("Nested literature cron marker")
            inside = True
        elif line == END:
            if not inside:
                raise ValueError("Unmatched literature cron end marker")
            inside = False
        elif not inside:
            kept.append(line)
    if inside:
        raise ValueError("Unmatched literature cron begin marker")
    return "\n".join(kept).rstrip() + "\n" + block + "\n"


def cron_block(project=PROJECT, runtime=None):
    project, runtime = Path(project), Path(runtime or Path(project) / "runtime")
    quote = shlex.quote
    watchdog = (
        f"LITERATURE_HOME={quote(str(runtime))} /bin/sh {quote(str(project / 'scripts/service_watchdog.sh'))}"
    )
    cli = (
        f"cd {quote(str(project))} && LITERATURE_HOME={quote(str(runtime))} "
        f"{quote(str(project / '.venv/bin/python'))} -m nutri_literature.cli"
    )
    tick = f"if [ ! -f {quote(str(runtime / 'maintenance.flag'))} ]; then {cli} tick; fi"
    health = cli + " health-check"
    return "\n".join(
        [
            BEGIN,
            f"@reboot /usr/bin/flock -n {quote(str(runtime / 'watchdog.lock'))} /bin/sh -c {quote(watchdog)} >>{quote(str(runtime / 'logs/watchdog.log'))} 2>&1",
            f"* * * * * /usr/bin/flock -n {quote(str(runtime / 'watchdog.lock'))} /bin/sh -c {quote(watchdog)} >>{quote(str(runtime / 'logs/watchdog.log'))} 2>&1",
            f"* * * * * /usr/bin/flock -n {quote(str(runtime / 'scheduler.lock'))} /bin/sh -c {quote(tick)} >>{quote(str(runtime / 'logs/scheduler.log'))} 2>&1",
            f"*/5 * * * * /usr/bin/flock -n {quote(str(runtime / 'health.lock'))} /bin/sh -c {quote(health)} >>{quote(str(runtime / 'logs/health.log'))} 2>&1",
            END,
        ]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime or args.project / "runtime"
    result = subprocess.run(
        ["crontab", "-l"], text=True, capture_output=True, check=False, env={**os.environ, "LC_ALL": "C"}
    )
    if result.returncode and "no crontab" not in result.stderr.lower():
        raise RuntimeError("Could not read crontab: " + result.stderr)
    existing = result.stdout if result.returncode == 0 else ""
    replacement = replace_block(existing, cron_block(args.project, runtime))
    if args.dry_run:
        print(replacement)
        return
    (runtime / "logs").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = runtime / f"crontab-before-{stamp}.txt"
    backup.write_text(existing, encoding="utf-8")
    os.chmod(backup, 0o600)
    subprocess.run(["crontab", "-"], input=replacement, text=True, check=True)
    print(f"Cron installed; previous crontab saved to {backup}")


if __name__ == "__main__":
    main()
