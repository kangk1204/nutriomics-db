from dataclasses import dataclass
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    home: Path
    config: Path

    @classmethod
    def from_env(cls):
        return cls(
            Path(os.environ.get("LITERATURE_HOME", PROJECT / "runtime")),
            Path(os.environ.get("LITERATURE_CONFIG", PROJECT / "config" / "topics.json")),
        )

    @property
    def database(self):
        return self.home / "data" / "literature.sqlite3"

    @property
    def raw(self):
        return self.home / "raw"

    @property
    def reports(self):
        return self.home / "reports"

    @property
    def backups(self):
        return self.home / "backups"

    @property
    def logs(self):
        return self.home / "logs"

    @property
    def dictionaries(self):
        return self.home / "dictionaries"

    def ensure(self):
        for path in [
            self.database.parent,
            self.raw,
            self.reports,
            self.backups,
            self.logs,
            self.dictionaries,
        ]:
            path.mkdir(parents=True, exist_ok=True)
