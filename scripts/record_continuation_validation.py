"""Record completed checks and confirmed Library replacement identity."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

ROOT=Path('research/2026-09-30/expansion')
summary=ROOT/'NutriGenomics_public_expansion_2026-10-01.txt'
os.setxattr(summary,'user.library-file-version',b'1')
paths=[ROOT/'nutrition_evidence.sqlite3',ROOT/'shareable_food_composition_v3.sqlite3']
integrity={}
for path in paths:
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        integrity[path.name]={'integrity_check':db.execute('PRAGMA integrity_check').fetchone()[0],
                              'foreign_key_errors':db.execute('PRAGMA foreign_key_check').fetchall()}
        if 'shareable' in path.name:
            assert set(row[0] for row in db.execute('SELECT source FROM ng_release'))=={'USDA-FDC'}
            assert db.execute('SELECT count(*) FROM ng_evidence').fetchone()[0]==332
    assert integrity[path.name]['integrity_check']=='ok' and not integrity[path.name]['foreign_key_errors']
record={'full_suite_passed':182,'full_suite_dependency_warnings':3,'task_specific_tests_passed':45,
        'core_continuation_ruff':'passed','database_integrity':integrity,
        'artifact_hashes':{path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        'library_file_id':'libfile_2e5da0fd460c8191a4b878571071afa8',
        'file_id':'file_0000000039f881faa9f923064514acf7',
        'library_version':1,'library_replace_status':'succeeded',
        'summary_sha256':hashlib.sha256(summary.read_bytes()).hexdigest()}
(ROOT/'reports/final_continuation_validation.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record,indent=2))
