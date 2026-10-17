import json

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.validate_quantitative_ranges import validate


def write(tmp_path, **changes):
    (tmp_path / 'effects').mkdir()
    row = {'series_accession': 'GSE_FIXTURE', 'feature_type': 'gene', 'feature_id': 'A',
           'contrast': 'test', 'analysis_version': 'v1', 'effect_estimate': 0.,
           'standard_error': None, 'p_value': None, 'q_value': None, 'n_people': 2,
           'n_samples': 4, 'effect_unit': 'log2 difference', **changes}
    pq.write_table(pa.Table.from_pylist([row]), tmp_path / 'effects/data.parquet')
    (tmp_path / 'manifest.json').write_text(json.dumps({'release_id': 'fixture', 'public_counts': {'effect': 1}}))


def test_measured_zero_and_missing_statistics_are_distinct(tmp_path):
    write(tmp_path)
    report = validate(tmp_path, tmp_path / 'qa.json')
    assert report['passed']
    assert report['missing_counts']['effect_estimate'] == 0
    assert report['missing_counts']['p_value'] == 1


def test_invalid_values_fail_without_silent_conversion(tmp_path):
    write(tmp_path, p_value=-.1, q_value=float('nan'), standard_error=-1., n_samples=1)
    report = validate(tmp_path, tmp_path / 'qa.json')
    assert not report['passed']
    for name in ('p_value_outside_0_1', 'q_value_nonfinite', 'negative_standard_error', 'people_exceed_samples'):
        assert report['invalid_counts'][name] == 1
    assert report['missing_counts']['q_value'] == 0
    assert report['source_values_modified'] is False
    json.loads((tmp_path / 'qa.json').read_text(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
