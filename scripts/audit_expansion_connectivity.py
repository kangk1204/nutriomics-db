"""Offline audit; conservative source-rights gate, no publication or deployment."""
import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
from pathlib import Path

ROOT = Path('research/2026-09-30/expansion')


def main():
    db = sqlite3.connect(f'file:{ROOT / "nutrition_evidence.sqlite3"}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    releases = [dict(r) for r in db.execute('SELECT * FROM ng_release')]
    rights = []
    for release in releases:
        verified = release['source'] == 'USDA-FDC'
        rights.append({
            'release_id': release['id'], 'source': release['source'],
            'access_status': release['access_status'],
            'redistribution_status': 'verified_public_domain' if verified else 'quarantined_unverified',
            'policy_url': 'https://fdc.nal.usda.gov/api-guide/' if verified else None,
            'basis': 'USDA FoodData Central CC0/public-domain API guide captured locally' if verified
            else 'Public access alone does not establish source-specific redistribution rights.',
        })
    raw = ROOT / 'measurements/GSE27385_series_matrix.txt.gz'
    text = gzip.decompress(raw.read_bytes()).decode('utf-8')
    header = text.split('!series_matrix_table_begin')[0]
    fields = {}
    for row in csv.reader(io.StringIO(header), delimiter='\t'):
        if row and row[0] in ('!Sample_geo_accession', '!Sample_description', '!Sample_data_processing', '!Sample_platform_id'):
            fields[row[0]] = row[1:]
    samples = []
    for accession, description, processing, platform in zip(
        fields['!Sample_geo_accession'], fields['!Sample_description'],
        fields['!Sample_data_processing'], fields['!Sample_platform_id'], strict=True
    ):
        match = re.fullmatch(r'HPBMC_(\d+)([ABC])_(FIBRATE|FISHOIL|PLACEBO)', description)
        if not match or 'RMA' not in processing or platform != 'GPL570':
            raise ValueError(f'Unexpected reviewed assay metadata: {accession}')
        samples.append(dict(accession=accession, participant_id=match[1], period=match[2],
                            treatment=match[3], processing=processing, platform=platform))
    people = {r['participant_id'] for r in samples}
    assert len(samples) == 33 and len(people) == 11
    for person in people:
        assays = [r for r in samples if r['participant_id'] == person]
        assert {r['treatment'] for r in assays} == {'FIBRATE', 'FISHOIL', 'PLACEBO'}
        assert {r['period'] for r in assays} == {'A', 'B', 'C'}
    layers = dict(db.execute('SELECT layer,count(*) FROM ng_evidence GROUP BY layer'))
    report = {
        'executor': 'Original authorized Windows workspace; no executor switch',
        'rights': rights,
        'rights_gate': 'Only verified releases may enter a shareable release; all others quarantined. No release published.',
        'expression': {
            'study': 'N06 / GSE27387; PBMC child GSE27385',
            'n_people': len(people), 'n_arrays': len(samples), 'n_features': 54675,
            'n_values': 1804275, 'processing': 'Source reports RMA in Bioconductor',
            'raw_or_derived': 'Source processed estimates; local operation only Parquet conversion',
            'units': 'Normalized expression estimates; header does not explicitly specify log base or physical units',
            'raw_CEL_downloaded': False, 'participant_overlap': 'Three conditions per person; paired crossover, not 33 independent people',
            'input_sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
            'assays': samples,
        },
        'connectivity': {
            'evidence_layers': layers,
            'food_to_nutrient_edges': layers.get('composition', 0),
            'nutrient_to_compound_mappings': 'Four candidate related mappings; not exact quantitative chemical identities',
            'molecule_to_target_edges': layers.get('molecular_target', 0),
            'pathway_edges': layers.get('pathway', 0),
            'verified_full_food_to_health_paths': 0,
            'health_context': 'Four DASH whole-diet aggregate BP contrasts; no individual food or molecule attribution',
            'expression_join': 'GPL570 probes have no audited gene/target mappings; disconnected from food and clinical evidence',
        },
        'candidate_priority': [
            {'accession': 'GSE323980', 'priority': 1, 'reason': 'Human randomized dietary comparison; 24 completers and 20 RNA-seq participants reported; paired IDs and count units require sample-level verification'},
            {'accession': 'GSE15284', 'priority': 2, 'reason': 'Human fish dietary intervention; participant identities and independent value remain unverified'},
            {'accession': 'GSE46761', 'priority': 3, 'reason': 'Diet microbiome intervention; repeated participants and microbial-versus-human molecular scope require verification'},
        ],
        'remaining_work': ['Official molecular target/pathway ingestion with verified rights',
                           'Candidate sample-level metadata and bounded measurement ingestion',
                           'Rights-gated shareable export with dependency closure'],
        'negative_findings_preserved': 'Inherited 484803 CpGs, BH q<.05=0; no new molecular effects or health benefits inferred',
        'production_deployed': False,
    }
    destination = ROOT / 'reports/connectivity_rights_expression_audit.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(destination)


if __name__ == '__main__':
    main()
