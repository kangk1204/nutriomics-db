import gzip
import json
from pathlib import Path
root=Path('research/2026-09-30/expansion/continuation')
for row in json.loads((root/'subset-manifest.json').read_text()):
    with gzip.open(root/row['file'],'rt') as stream:
        print(row['file'],[stream.readline().strip() for _ in range(3)])
    if row['study']=='GSE323980':
        import pandas as pd
        frame=pd.read_csv(root/row['file'])
        print('non-Ensembl rows:',int((~frame['Geneid'].str.fullmatch(r'ENSG\d+(?:\.\d+)?')).sum()))
        print('missing quantitative values:',int(frame.iloc[:,1].isna().sum()),'negative:',int((frame.iloc[:,1]<0).sum()))
