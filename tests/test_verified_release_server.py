import hashlib
import json

import pytest

from scripts.serve_verified_release import verify


@pytest.mark.parametrize('application,filename', [('internal', 'explorer.sqlite'), ('public', 'nutriomics.sqlite3')])
def test_verifies_actual_immutable_bytes_before_readiness(tmp_path, application, filename):
    database = tmp_path / filename
    database.write_bytes(b'fixture release bytes')
    expected = hashlib.sha256(database.read_bytes()).hexdigest()
    (tmp_path / 'manifest.json').write_text(json.dumps({'release_id': 'fixture', 'database_sha256': expected}))
    receipt = tmp_path / 'startup.json'
    assert verify(tmp_path, application, receipt)['database_sha256'] == expected
    receipt.unlink()
    database.write_bytes(b'changed bytes')
    with pytest.raises(ValueError, match='SHA-256'):
        verify(tmp_path, application, receipt)
    assert not receipt.exists()
