import json
from pathlib import Path

from nutrigenomics.public_quality import analysis_role
from scripts.rebuild_public_omics import SUFFIX, rebuilt_policy, recipe


def test_fourteen_outcomes_and_dependency_order_are_fixed(tmp_path):
    source = Path(__file__).resolve().parents[1]
    plan = recipe(tmp_path, source)
    assert len(plan['outcomes']) == 14
    assert len({(r['series'], r['analysis_version']) for r in plan['outcomes']}) == 14
    assert all(r['analysis_version'].endswith(SUFFIX) for r in plan['outcomes'])
    known = set()
    for job in plan['jobs']:
        assert set(job['requires']) <= known
        assert len(job['source_sha256']) == 64
        known.add(job['id'])
    assert not plan['former_server_dependency']
    assert not plan['historical_exact_reproduction_claim']
    policy = json.loads(rebuilt_policy(source, tmp_path, plan).read_text())
    allowed = {(r['series_accession'], r['analysis_version']) for r in policy['analyses'] if r.get('export_allowed')}
    assert allowed == {(r['series'], r['analysis_version']) for r in plan['outcomes']}
    assert not any(s.startswith('E-MTAB') for s, _ in allowed)
    assert analysis_role('gse56960-rma-limma-v1' + SUFFIX) == 'historical'
