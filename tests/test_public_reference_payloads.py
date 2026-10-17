import json
import sqlite3

import pytest

from nutrigenomics.publication import reference_node_metadata, source_reference
from scripts.sanitize_public_references import metadata_only_authorizer


def test_allowed_mapping_does_not_license_its_restricted_dictionary():
    original = {'source_id': 'restricted', 'structure_verified': False,
                'dictionary': {'Definition': 'Synthetic restricted text'}}
    safe = reference_node_metadata(original, {'restricted': {'export_allowed': False, 'url': 'https://example.org/source', 'version': 'v1'}})
    assert 'dictionary' not in safe and safe['withheld_source_content']['url'] == 'https://example.org/source'
    assert 'dictionary' in original
    assert reference_node_metadata(original, {'restricted': {'export_allowed': True}}) == original


def test_citation_keeps_official_url_and_release_fallback():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE source(id TEXT,metadata_json TEXT)')
    db.execute('INSERT INTO source VALUES(?,?)', ('synthetic', json.dumps({'source_url': 'https://example.org/official', 'release': '2026-04', 'export_allowed': True})))
    value = source_reference(db, db.execute('SELECT * FROM source').fetchone())
    assert value['url'] == 'https://example.org/official' and value['version'] == '2026-04'
    db.close()


def test_metadata_repair_cannot_mutate_quantitative_tables():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE effect(value REAL)')
    db.execute('INSERT INTO effect VALUES(.25)')
    db.execute('CREATE TABLE node(id TEXT,metadata_json TEXT)')
    db.execute("INSERT INTO node VALUES('n','{}')")
    db.set_authorizer(metadata_only_authorizer)
    db.execute("UPDATE node SET metadata_json='{}' WHERE id='n'")
    with pytest.raises(sqlite3.DatabaseError, match='authorized'):
        db.execute('UPDATE effect SET value=0')
    assert db.execute('SELECT value FROM effect').fetchone()[0] == .25
    db.close()
