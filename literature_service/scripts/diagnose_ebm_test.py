"""Audit EBM native member names and paper-ID overlap without reading test labels."""
import json
import re
import tarfile

from nutri_literature.benchmarks import _receipt
from nutri_literature.db import digest, dumps, now, uid
from nutri_literature.public_corpora import _read_source
from nutri_literature.settings import Settings


if __name__=='__main__':
    settings=Settings.from_env()
    index=json.loads((settings.home/'public_benchmarks'/'v2-measurement-index.json').read_bytes())
    prepared=index['steps']['prepare:development:ebmnlp:abstract']
    from pathlib import Path
    directory=Path(prepared['inputs_path']).parent
    manifest=json.loads((directory/'manifest.json').read_bytes())
    raw=(directory/'source.release').read_bytes()
    if digest(raw)!=manifest['source_sha256']:
        raise ValueError('Native release differs from pinned archive')
    identifiers=set()
    task_ids={kind:set() for kind in ['participants','interventions','outcomes']}
    with tarfile.open(directory/'source.release','r:*') as archive:
        for name in archive.getnames():
            matched=re.search(r'aggregated/starting_spans/(participants|interventions|outcomes)/test/gold/(\d+)\.AGGREGATED\.ann$',name)
            if matched:
                task_ids[matched[1]].add(matched[2])
                identifiers.add(matched[2])
    # The frozen parser currently returns no test documents; do not alter it.
    parsed,member,_=_read_source('ebmnlp',raw,'test','abstract')
    overlaps={}
    for path in (settings.home/'public_benchmarks').glob('*/*/inputs.json'):
        value=json.loads(path.read_bytes())
        if value.get('split')!='test':
            shared=identifiers & {str(p['pmid']) for p in value['documents']}
            if shared:
                overlaps.setdefault(value['corpus'],set()).update(shared)
    record={'schema_version':'literature-public-corpus-diagnostic-v1','id':uid(),'at':now(),
            'corpus':'ebmnlp','source_sha256':digest(raw),'source_member':member,
            'status':'native_test_layout_unsupported_in_frozen_protocol' if not parsed and identifiers else 'requires_review',
            'native_expert_test_papers':len(identifiers),'native_expert_test_tasks':{key:len(value) for key,value in task_ids.items()},
            'parsed_test_papers':len(parsed),'development_overlap_pmids':{key:sorted(value) for key,value in overlaps.items()},
            'method':'Expert test filenames and prepared input paper identifiers; no expert label values inspected',
            'disposition':'Do not measure this protected partition in the frozen protocol; preserve other corpora'}
    target=settings.home/'validation'/'ebm-test-diagnostic.json'
    target.write_text(dumps(record),encoding='utf-8')
    _receipt(settings,'public_corpus_diagnostic',record['id'],record)
    print(dumps(record))
