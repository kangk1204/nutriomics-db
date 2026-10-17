"""Prepare independently downloaded public inputs in a new project work directory."""
import argparse
import json
from pathlib import Path

from nutrigenomics.db import get_session_factory
from nutrigenomics.models import GeoSeries
from nutrigenomics.source_updates import atomic_json
from scripts.audit_geo_design import audit_database
from scripts.audit_gse127530_counts import audit
from scripts.download_bridge_studies import fetch
from scripts.extract_geo_archives import extract_archives
from scripts.import_fdc_json import import_fdc
from scripts.import_geo_family_soft import import_family_soft
from scripts.import_human_bridge import import_bridge


def prepare(work, source):
    work, source = Path(work).resolve(), Path(source).resolve()
    data = work / 'data'
    (data / 'db').mkdir(parents=True, exist_ok=True)
    url = 'sqlite:///' + str(data / 'db/nutrigenomics.sqlite3')
    results = {'bridge': import_bridge(source / 'config/human_exposures.v1.json', url)}
    with get_session_factory(url)() as session:
        if session.get(GeoSeries, 'GSE54690') is None:
            parent = session.get(GeoSeries, 'GSE54325')
            session.add(GeoSeries(accession='GSE54690', study_id=parent.study_id,
                title='Flavanol SuperSeries; same study, no extra cohort',
                metadata_verified=False, assay_data_downloaded=False))
            session.commit()
    results['family_metadata'] = import_family_soft(data, url)
    results['design_audit'] = audit_database(url, work / 'outputs/geo-design-audit')
    results['corrected_counts_audit'] = audit(data, work / 'outputs/gse127530-v1')
    results['extraction'] = extract_archives(data,
        ['GSE54325', 'GSE54643', 'GSE107205', 'GSE219217', 'GSE56960'], 30)
    cdf = work / 'raw/GSE13466/GPL7144_HsNuGOHs1a520180_Hs_ENTREZG.cdf.gz'
    result = fetch('https://ftp.ncbi.nlm.nih.gov/geo/platforms/GPL7nnn/GPL7144/suppl/' + cdf.name,
                   cdf, 16 * 1024**2)
    atomic_json(work / 'custom-cdf-source.json', result)
    results['fdc'] = import_fdc(data, url)
    receipt = {'status': 'completed', 'former_server_dependency': False,
        'core': str(data / 'db/nutrigenomics.sqlite3'), 'results': results,
        'source_scope': 'Official FDC/GEO originals; expert design review remains pending'}
    atomic_json(work / 'input-preparation.json', receipt)
    return receipt


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work', type=Path, required=True)
    p.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    a = p.parse_args()
    print(json.dumps(prepare(a.work, a.source)))
