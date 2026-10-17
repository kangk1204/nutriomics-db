import gzip
import json
import sqlite3

import pytest

from nutrigenomics.explorer_build import sha256
from scripts.qualify_rebuild_storage import qualify
from scripts.verify_reacquired_accuracy import numeric_outputs


def test_closed_storage_reopens_and_checks_all_fixture_values(tmp_path):
    result = qualify(tmp_path / 'qualification', rows=1000)
    assert result['status'] == 'passed'
    assert len(result['reopened_reads']) == 3


@pytest.mark.parametrize('alteration', [None, 'statistic', 'duplicate'])
def test_rebuilt_numeric_audit_preserves_nulls_and_detects_value_or_multiplicity_changes(tmp_path, alteration):
    work = tmp_path / 'work'
    folder = work / 'outputs/run'
    folder.mkdir(parents=True)
    version = 'synthetic-rebuild-v2'
    (work / 'analysis-rebuild-plan.json').write_text(json.dumps({'outcomes': [
        {'series': 'SYNTHETIC', 'analysis_version': version, 'folder': 'run'}]}))
    columns = ['feature_type', 'feature_id', 'contrast', 'effect_estimate', 'standard_error', 'p_value', 'q_value', 'n_people', 'n_samples']
    with gzip.open(folder / 'gene_effects.tsv.gz', 'wt') as stream:
        stream.write('\t'.join(columns) + '\n')
        stream.write('gene\tA\tpaired\t-0.25\tNA\t0.4\t0.72\t5\t45\n')
    (folder / 'analysis_metadata.json').write_text(json.dumps({'result_files': ['gene_effects.tsv.gz']}))
    (folder / 'DONE.json').write_text(json.dumps({'files': [{'path': 'gene_effects.tsv.gz',
        'sha256': sha256(folder / 'gene_effects.tsv.gz')}]}))
    core = work / 'core.sqlite3'
    with sqlite3.connect(core) as db:
        db.execute('CREATE TABLE study_effect(series_accession,analysis_version,feature_type,feature_id,contrast,effect_estimate,standard_error,p_value,q_value,n_people,n_samples)')
        row = ('SYNTHETIC', version, 'gene', 'A', 'paired', -.25, None, .4, .72, 5, 45)
        db.execute('INSERT INTO study_effect VALUES(?,?,?,?,?,?,?,?,?,?,?)', row)
        if alteration == 'statistic':
            db.execute('UPDATE study_effect SET q_value=0')
        elif alteration == 'duplicate':
            db.execute('INSERT INTO study_effect VALUES(?,?,?,?,?,?,?,?,?,?,?)', row)
    output = work / 'audit.json'
    if alteration:
        with pytest.raises(ValueError, match='differ from core'):
            numeric_outputs(work, core, output)
    else:
        result = numeric_outputs(work, core, output)
        assert result['status'] == 'passed' and result['rows'] == 1
