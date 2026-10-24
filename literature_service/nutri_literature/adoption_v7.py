"""ID adoption uses nested held-out errors, not the optimized OOF headline."""
import json
from pathlib import Path
from .contract_v7 import immutable,sha


def identity_adoption(manifest):
    rows=manifest['outer_metrics']
    total={key:sum(r.get(key,0) for r in rows) for key in ('tp','fp','fn','changed','correct_changes','damaged_old_correct')}
    precision=total['tp']/max(1,total['tp']+total['fp'])
    correction_precision=total['correct_changes']/max(1,total['changed'])
    reasons=[]
    if precision<.9:reasons.append('nested_selected_id_precision_below_90_percent')
    if total['changed']==0 or correction_precision<.9:reasons.append('nested_changed_id_precision_not_demonstrated')
    if total['damaged_old_correct']:reasons.append('previous_correct_ids_damaged')
    # Clearing a previously linked ID must also be evaluated in the strict
    # native-ID endpoint before a model may rewrite stored automatic identities.
    if not manifest.get('strict_boundary_id_preservation_verified'):
        reasons.append('strict_boundary_id_preservation_not_verified')
    return {'passed':not reasons,'counts':total,'precision':precision,'changed_id_precision':correction_precision,
            'reasons':reasons,'nutrition_accuracy':'unmeasured'}


def write_identity_adoption(directory):
    directory=Path(directory);raw=(directory/'manifest.json').read_bytes()
    result={**identity_adoption(json.loads(raw)),'model_manifest_sha256':sha(raw)}
    immutable(directory/'adoption.json',result)
    return result
