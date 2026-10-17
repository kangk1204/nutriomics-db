"""A successful HTTP export alone cannot establish permissions or selection parity."""
import json

import pytest

from scripts.verify_explorer_release_flows import parse_html_snapshot, validate_snapshot


def test_export_selection_and_permission_audit_rejects_dropped_and_raw_restricted_edges():
    graph={'release_id':'fixture','nodes':[{'id':'f'},{'id':'g'}],'edges':[{'id':'e'}]}
    snapshot={**graph,'edges':[{'id':'e','tier':'restricted','export_allowed':False,'relation':'restricted_source_reference'}]}
    assert validate_snapshot(snapshot,graph)==[]
    snapshot['edges'][0]['relation']='raw_interaction'
    snapshot['nodes']=snapshot['nodes'][:1]
    failures=validate_snapshot(snapshot,graph)
    assert 'selection:nodes' in failures and 'restricted_edge:e' in failures


def test_unassessed_omics_values_cannot_be_exported_as_permitted():
    graph={'release_id':'fixture','nodes':[{'id':'g'}],'edges':[]}
    data={**graph,'node_details':{'g':{'effects':[{'effect_estimate':.2,'export_allowed':False}]}}}
    assert 'unpermitted_detail:g:effects' in validate_snapshot(data,graph)


def test_html_requires_embedded_runtime_and_network_block():
    data={'nodes':[],'release_id':'fixture'}
    html="<meta content=\"connect-src 'none'\"><script>window.__NUTRIOMICS_SNAPSHOT__="+json.dumps(data)+";</script><script>inline</script>"
    assert parse_html_snapshot(html)==data
    with pytest.raises(ValueError):
        parse_html_snapshot(html.replace("connect-src 'none'","connect-src *"))
    with pytest.raises(ValueError):
        parse_html_snapshot(html+'<script src="https://unavailable.invalid/runtime.js"></script>')


def test_export_context_uses_frozen_case_identity(tmp_path, monkeypatch):
    import scripts.verify_explorer_release_flows as flows
    from scripts.benchmark_explorer_performance import digest
    seed = 'synthetic:current-food'
    cases = [{'id': 'maximum-graph', 'params': {'seed_id': seed, 'max_nodes': 250, 'max_edges': 750}}]
    spec = tmp_path / 'cases.json'
    spec.write_text(json.dumps({'cases': cases, 'case_sha256': digest(cases)}))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text('{}')
    graph = {'release_id': 'synthetic-release', 'nodes': [{'id': seed, 'kind': 'food'}], 'edges': []}
    (tmp_path / 'maximum-graph-response.json').write_text(json.dumps(graph))
    payloads = []

    def capture(base, path, payload):
        payloads.append(payload)
        return {'status': 503}, b''

    monkeypatch.setattr(flows, 'request', capture)
    report = flows.exports('http://unused.invalid', tmp_path, spec, manifest, tmp_path / 'output', 'maximum-graph')
    assert len(payloads) == 3
    assert all(p['filters']['scientific_context'] == {'seed_id': seed, 'intake_g': 150} for p in payloads)
    assert report['maximum_selection_verified'] is False
    assert report['display_limit_valid'] is True
