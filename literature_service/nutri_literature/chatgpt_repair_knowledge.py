"""Official annotation, chemical metadata and conservative identifier history."""
import gzip
from pathlib import Path
import re

from .chatgpt_repair_io import HOME, ROOT, log
from .chatgpt_evaluation import _read
from .chatgpt_grounding import key
from .contract_v7 import immutable, sha, canonical


def fetch(url, filename, params=None, maximum=220*1024*1024):
    import requests
    path = HOME / 'knowledge' / filename
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.partial')
        size = 0
        with requests.get(url, params=params, stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            with temporary.open('wb') as stream:
                for chunk in response.iter_content(1024*1024):
                    size += len(chunk)
                    if size > maximum:
                        raise ValueError('Official knowledge source exceeds bounded download size')
                    stream.write(chunk)
        temporary.replace(path)
    return path, {'url': url, 'parameters': params, 'file': filename, 'sha256': sha(path.read_bytes())}


def parse_promotions(text):
    result = []
    for line in text.splitlines():
        cells = re.split(r'\s{2,}', line.strip())
        if len(cells) == 3 and re.fullmatch(r'C\d+', cells[0]):
            result.append({'old_id': cells[0], 'old_name': cells[1], 'replacement_name': cells[2],
                           'change': 'official_record_promotion', 'chemical_identity_proven': False})
    return result


def prepare():
    directory = HOME / 'knowledge'
    if (directory / 'manifest.json').exists():
        manifest = _read(directory / 'manifest.json')
        for filename, checksum in manifest['artifacts'].items():
            if sha((directory / filename).read_bytes()) != checksum:
                raise ValueError('Frozen official knowledge artifact changed')
        return manifest
    from pypdf import PdfReader
    sources = []
    guide, receipt = fetch('https://ftp.ncbi.nlm.nih.gov/pub/lu/BioRED/BioRED_Annotation_Guideline.pdf', 'biored-guideline.pdf')
    sources.append(receipt)
    guide_text = '\n'.join(page.extract_text() for page in PdfReader(guide).pages)
    (directory / 'biored-guideline.txt').write_text(guide_text, encoding='utf-8')
    pdf, receipt = fetch('https://www.nlm.nih.gov/mesh/2016/download/TermChange2016.pdf', 'mesh-changes-2016.pdf')
    sources.append(receipt)
    promotion_text = '\n'.join(page.extract_text(extraction_mode='layout') for page in PdfReader(pdf).pages)
    promotion = parse_promotions(promotion_text)
    path, receipt = fetch('https://datadiscovery.nlm.nih.gov/resource/7vzs-mg28.json', 'mesh-changes-2023-2026.json', {'$limit': 50000})
    sources.append(receipt)
    changes = _read(path)
    if not isinstance(changes, list) or len(changes) >= 50000:
        raise ValueError('MeSH history pagination must be complete')
    promotion += [{'old_id': r['replaced_ui'], 'old_name': r['replaced_name'],
                   'replacement_id': r['replacement_option_ui'], 'replacement_name': r['replacement_name'],
                   'change': r['note'], 'chemical_identity_proven': False} for r in changes]
    raw, receipt = fetch('https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene_history.gz', 'gene-history.gz')
    sources.append(receipt)
    genes = {}
    with gzip.open(raw, 'rt', encoding='utf-8') as stream:
        for line in stream:
            if line.startswith('#'):
                continue
            taxon, active, discontinued, symbol, date = line.rstrip('\n').split('\t')[:5]
            if int(taxon) in {9606, 10090, 10116}:
                genes[discontinued] = {'current_id': None if active == '-' else active,
                                       'taxon': int(taxon), 'previous_symbol': symbol, 'date': date,
                                       'identity_proven': active != '-'}
    snapshot = _read(ROOT / 'runtime/v6/models/mesh-snapshot.json')
    mesh_path = ROOT / 'runtime/v6/models/mesh' / snapshot['filename']
    if sha(mesh_path.read_bytes()) != snapshot['sha256']:
        raise ValueError('Pinned full MeSH metadata differs')
    mesh = _read(mesh_path)
    metadata = {}
    for row in mesh['records']:
        metadata[row['native_id']] = {k: row.get(k) for k in ('name', 'record_kind', 'scr_class', 'chemical_candidate', 'trees')}
    immutable(directory / 'gene-history.json', genes)
    immutable(directory / 'mesh-history.json', {'records': promotion, 'identity_policy': 'replacement and hierarchy do not establish chemical equality'})
    immutable(directory / 'mesh-metadata.json', metadata)
    names = ('biored-guideline.pdf', 'biored-guideline.txt', 'mesh-changes-2016.pdf',
             'mesh-changes-2023-2026.json', 'gene-history.gz', 'gene-history.json', 'mesh-history.json', 'mesh-metadata.json')
    manifest = {'sources': sources, 'mesh_metadata_source': str(mesh_path.relative_to(ROOT)),
                'mesh_metadata_source_sha256': snapshot['sha256'], 'mesh_records': len(metadata),
                'gene_history_records': len(genes), 'mesh_history_records': len(promotion),
                'chemical_historical_identity_mappings': 0,
                'chemical_history_limitation': '2016 and2023-2026 replacements only; historical CAS identity unproven, no automatic chemical equivalence',
                'supported_taxa': [9606, 10090, 10116], 'code_sha256': sha(Path(__file__).read_bytes()),
                'artifacts': {n: sha((directory / n).read_bytes()) for n in names}}
    immutable(directory / 'manifest.json', manifest)
    log('knowledge-ready', mesh_records=len(metadata), gene_history_records=len(genes), mesh_history_records=len(promotion))
    return manifest


class Knowledge:
    def __init__(self, catalog):
        self.catalog = catalog
        self.metadata = _read(HOME / 'knowledge/mesh-metadata.json')
        self.genes = _read(HOME / 'knowledge/gene-history.json')
        history = _read(HOME / 'knowledge/mesh-history.json')['records']
        self.chemical_by_name = {}
        for r in history:
            for name in (r['old_name'], r['replacement_name']):
                self.chemical_by_name.setdefault(key(name), []).append(r)
        self.records_by_id = {}
        for r in catalog.records:
            self.records_by_id.setdefault((r['kind'], r['native_id']), []).append(r)
        self.gene_by_name = {}
        for identifier, record in self.genes.items():
            if record['identity_proven'] and ('gene', record['current_id']) in self.records_by_id and record['previous_symbol'] != '-':
                self.gene_by_name.setdefault(key(record['previous_symbol']), []).append((identifier, record))

    def enrich(self, mention, source):
        candidates = self.catalog.candidates(mention, source)
        values = []
        for c in candidates:
            record = {k: c.get(k) for k in ('native_id', 'taxon', 'names')}
            if mention['kind'] == 'chemical':
                record['mesh_metadata'] = self.metadata.get(c['native_id'])
            values.append(record)
        if mention['kind'] == 'chemical':
            for r in self.chemical_by_name.get(key(mention['text']), []):
                if not any(v['native_id'] == r['old_id'] for v in values):
                    values.append({'native_id': r['old_id'], 'taxon': None, 'names': [r['old_name']],
                                   'historical_record': True, 'replacement_name': r['replacement_name'],
                                   'replacement_is_not_chemical_equality': True})
        else:
            for identifier, r in self.gene_by_name.get(key(mention['text']), []):
                if not any(v['native_id'] == identifier for v in values):
                    values.append({'native_id': identifier, 'taxon': r['taxon'],
                                   'names': [r['previous_symbol']], 'historical_record': True,
                                   'current_id': r['current_id'], 'official_same_gene_merge': True})
            for value in values:
                if value['native_id'] in self.genes:
                    value['gene_history'] = self.genes[value['native_id']]
        # Label-free order; learned ID priorities never enter independent prompts.
        return sorted(values, key=lambda v: canonical(v))


if __name__ == '__main__':
    prepare()
