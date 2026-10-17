"""Bounded project-only write/reopen/integrity qualification; no global cache changes."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from nutrigenomics.source_updates import atomic_json


def qualify(output, rows=250000):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    database = output / 'qualification.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('PRAGMA cache_size=-32768')
        db.executescript('CREATE TABLE probe(id TEXT PRIMARY KEY,component TEXT,payload TEXT); CREATE INDEX by_component ON probe(component);')
        expected = hashlib.sha256()
        for start in range(0, rows, 5000):
            batch = []
            for i in range(start, min(rows, start + 5000)):
                row = (hashlib.sha256(str(i).encode()).hexdigest(), 'FDCNutrient:' + str(i % 237),
                       json.dumps({'fixture': True, 'i': i, 'text': 'source identifier integrity sentinel'}, sort_keys=True))
                batch.append(row)
                expected.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
            db.executemany('INSERT INTO probe VALUES(?,?,?)', batch)
    attempts = []
    for _ in range(3):
        actual = hashlib.sha256()
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
            db.execute('PRAGMA cache_size=-32768')
            integrity = [r[0] for r in db.execute('PRAGMA integrity_check')]
            for row in db.execute('SELECT * FROM probe ORDER BY rowid'):
                actual.update(json.dumps(row, separators=(',', ':')).encode() + b'\n')
        attempts.append({'integrity': integrity, 'values_match': actual.digest() == expected.digest()})
    result = {'status': 'passed' if all(r['integrity'] == ['ok'] and r['values_match'] for r in attempts) else 'failed',
              'rows': rows, 'reopened_reads': attempts, 'scope': 'Synthetic storage qualification, not biological data validation'}
    atomic_json(output / 'qualification.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = qualify(a.output)
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'passed' else 1)
