"""Audit public result domains and missingness without repairing source values."""
import argparse
import json
import math
from collections import Counter
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from nutrigenomics.explorer_build import sha256
from nutrigenomics.publication import KEY_FIELDS
from nutrigenomics.source_updates import atomic_json

NUMERIC = ('effect_estimate', 'standard_error', 'p_value', 'q_value', 'n_people', 'n_samples')


def validate(release, output):
    release = Path(release)
    manifest = json.loads((release / 'manifest.json').read_text())
    expected = manifest['public_counts']['effect']
    rows, missing, invalid, types, examples = 0, Counter(), Counter(), Counter(), []
    columns = list(dict.fromkeys([*KEY_FIELDS, *NUMERIC, 'effect_unit']))

    def count(mask):
        return pc.sum(pc.cast(pc.fill_null(mask, False), 'int64')).as_py() or 0

    for path in sorted((release / 'effects').glob('*.parquet')):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=65536, columns=columns, use_threads=False):
            rows += batch.num_rows
            checks = {}
            for key in NUMERIC:
                value = batch.column(key)
                missing[key] += value.null_count
                checks[key + '_nonfinite'] = pc.invert(pc.is_finite(value))
                if key in ('p_value', 'q_value'):
                    checks[key + '_outside_0_1'] = pc.or_(pc.less(value, 0), pc.greater(value, 1))
                if key == 'standard_error':
                    checks['negative_standard_error'] = pc.less(value, 0)
                if key in ('n_people', 'n_samples'):
                    checks[key + '_nonpositive'] = pc.less_equal(value, 0)
            checks['people_exceed_samples'] = pc.greater(batch.column('n_people'), batch.column('n_samples'))
            missing['effect_unit'] += batch.column('effect_unit').null_count
            for entry in pc.value_counts(batch.column('feature_type')).to_pylist():
                types[str(entry['values'])] += entry['counts']
            for key, mask in checks.items():
                found = count(mask)
                invalid[key] += found
                if found and len(examples) < 20:
                    for row in batch.filter(pc.fill_null(mask, False)).slice(0, 20-len(examples)).to_pylist():
                        # Preserve the invalid raw file, but keep diagnostic
                        # JSON standards-compliant for downstream gate readers.
                        examples.append({'check': key, **{k: {'nonfinite_value': repr(v)} if isinstance(v, float) and not math.isfinite(v) else v for k, v in row.items()}})
    invalid['row_count_mismatch'] = int(rows != expected)
    passed = not any(invalid.values())
    report = {'passed': passed, 'status': 'passed' if passed else 'failed', 'release_id': manifest['release_id'],
              'source_manifest_sha256': sha256(release / 'manifest.json'), 'rows': rows, 'expected_rows': expected,
              'feature_type_counts': dict(types), 'missing_counts': dict(missing), 'invalid_counts': dict(invalid),
              'first_invalid_records': examples, 'source_values_modified': False,
              'scope': 'Numeric domain and count consistency only. Missing values remain missing; no biological validity or causal claim.'}
    atomic_json(Path(output), report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    report = validate(a.release, a.output)
    print(json.dumps({'status': report['status'], 'rows': report['rows'], 'invalid': report['invalid_counts']}))
    raise SystemExit(0 if report['passed'] else 1)
