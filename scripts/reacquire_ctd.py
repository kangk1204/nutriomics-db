"""Reacquire official CTD artifacts for restricted local research; never infer rights."""
import argparse
import csv
import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

from nutrigenomics.explorer_build import sha256
from nutrigenomics.source_updates import atomic_json

FILES = ('CTD_curated_chemicals_diseases.csv.gz', 'CTD_curated_genes_diseases.csv.gz',
         'CTD_chem_gene_ixns.csv.gz', 'CTD_genes_pathways.csv.gz', 'CTD_chemicals.csv.gz', 'CTD_diseases.csv.gz')


def acquire(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'files': {}, 'failures': {}}
    for name in FILES:
        previous = manifest['files'].get(name)
        if previous and sha256(output / previous['parquet']) == previous['parquet_sha256']:
            continue
        url = 'https://ctdbase.org/reports/' + name
        raw, partial = output / name, output / (name + '.part')
        try:
            if not raw.exists():
                with requests.get(url, timeout=(20, 90), stream=True) as response:
                    response.raise_for_status()
                    digest, count = hashlib.sha256(), 0
                    with partial.open('wb') as stream:
                        for chunk in response.iter_content(1024 * 1024):
                            count += len(chunk)
                            if count > 4 * 1024**3:
                                raise ValueError('Artifact exceeds 4 GiB source budget')
                            digest.update(chunk)
                            stream.write(chunk)
                    if partial.open('rb').read(2) != b'\x1f\x8b':
                        raise ValueError('Expected source gzip, received another response type')
                    partial.rename(raw)
            target = output / (name.replace('.csv.gz', '.parquet'))
            building = target.with_suffix('.parquet.building')
            columns, records, rows, writer = None, [], 0, None
            try:
                with gzip.open(raw, 'rt', encoding='utf-8', newline='') as stream:
                    for line in stream:
                        if line.startswith('#'):
                            candidate = next(csv.reader([line.lstrip('#').strip()]))
                            if len(candidate) > 2 and any(x in candidate for x in ('ChemicalID', 'GeneID', 'DiseaseID')):
                                columns = candidate
                            continue
                        if not line.strip():
                            continue
                        if columns is None:
                            raise ValueError('No explicit CTD header found')
                        values = next(csv.reader([line]))
                        if len(values) != len(columns):
                            raise ValueError('CTD column count mismatch')
                        records.append(dict(zip(columns, values, strict=True)))
                        if len(records) >= 25000:
                            table = pa.Table.from_pylist(records, schema=pa.schema([pa.field(k, pa.string()) for k in columns]))
                            if writer is None:
                                writer = pq.ParquetWriter(building, table.schema, compression='zstd')
                            writer.write_table(table)
                            rows += len(records)
                            records = []
                    if records:
                        table = pa.Table.from_pylist(records, schema=pa.schema([pa.field(k, pa.string()) for k in columns]))
                        if writer is None:
                            writer = pq.ParquetWriter(building, table.schema, compression='zstd')
                        writer.write_table(table)
                        rows += len(records)
            finally:
                if writer is not None:
                    writer.close()
            if not rows:
                raise ValueError('Empty CTD artifact')
            building.rename(target)
            manifest['files'][name] = {'id': 'CTD:' + sha256(raw)[:20], 'source': 'CTD',
                'source_url': url, 'version': datetime.now(UTC).isoformat(),
                'sha256': sha256(raw), 'bytes': raw.stat().st_size,
                'parquet': target.name, 'parquet_sha256': sha256(target), 'rows': rows,
                'columns': columns, 'export_allowed': False, 'license': 'redistribution-unconfirmed',
                'restriction_reason': 'Local noncommercial research; redistribution permission requires source-specific confirmation'}
            manifest['failures'].pop(name, None)
        except (OSError, ValueError, requests.RequestException) as error:
            manifest['failures'][name] = {'url': url, 'error': str(error)}
        manifest['status'] = 'completed' if len(manifest['files']) == len(FILES) else 'incomplete'
        atomic_json(manifest_path, manifest)
        print(json.dumps({'file': name, 'status': 'downloaded' if name in manifest['files'] else 'unavailable'}), flush=True)
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = acquire(a.output)
    raise SystemExit(0 if result['status'] == 'completed' else 1)
