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
