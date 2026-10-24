"""Lightweight cron observation without migrations, models or extraction work."""

from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from nutri_literature.db import dumps
    from nutri_literature.operations_v5 import observe
    from nutri_literature.settings import Settings

    settings = Settings.from_env()
    for attempt in range(30):
        try:
            print(dumps(observe(settings)), flush=True)
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 29:
                raise
            time.sleep(0.5)


if __name__ == "__main__":
    main()
