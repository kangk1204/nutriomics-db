import gzip
import csv
import io
import json
from pathlib import Path
root = Path('research/2026-09-30/expansion/continuation')
text = gzip.decompress((root/'GSE15284_series_matrix.txt.gz').read_bytes()).decode()
header = text.split('!series_matrix_table_begin')[0]
for row in csv.reader(io.StringIO(header), delimiter='\t'):
    if row and row[0] in ['!Sample_title','!Sample_description','!Sample_characteristics_ch1','!Sample_data_processing','!Sample_geo_accession']:
        print(row[0], json.dumps(row[1:])[:10000])
for acc in ['Q02809','Q9P2J5']:
    data=json.loads((root/f'uniprot-{acc}.json').read_text())
    print(acc, data['entryAudit'], [r for r in data['uniProtKBCrossReferences'] if r['database'] in ['GeneID','GO']])
