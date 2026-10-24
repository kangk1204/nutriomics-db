"""Offline provenance and native-metric audit of the complete tuning experiment."""
from pathlib import Path
import json
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from nutri_literature import chatgpt_tuning as base,chatgpt_joint_verifier as joint,benchmarks
    from nutri_literature.chatgpt_grounding import Catalog
    from nutri_literature.chatgpt_extraction import source_input,INSTRUCTIONS
    from nutri_literature.chatgpt_evaluation import _read,_settings,_metric
    from nutri_literature.contract_v7 import canonical,sha,immutable
    base_audit=_read(base.HOME/'completion-audit.json')
    if base_audit['status']!='complete' or base_audit['real_completed_requests']!=280:
        raise ValueError('The base280-response audit must complete first')
    contract=_read(joint.HOME/'contract.json')
    if (contract['implementation_sha256']!=sha(Path(joint.__file__).read_bytes())
            or contract['review_prompt_sha256']!=sha(joint.PROMPT)
            or contract['original_prompt_sha256']!=sha(INSTRUCTIONS)
            or contract['base_contract_sha256']!=sha((base.HOME/'contract.json').read_bytes())
            or contract['calibration_gold_sha256']!=sha((base.HOME/'inputs/calibration-gold.json').read_bytes())):
        raise ValueError('Joint verifier contract or source changed')
    calibration=_read(base.HOME/'inputs/calibration-gold.json')['documents']
    development=_read(base.HOME/'inputs/development-sources.json')['documents']
    docs={d['pmid']:d for d in [*calibration,*development]}
    catalog=Catalog.load(ROOT/'runtime/chatgpt-inputs/official-id-dictionary.json.gz')
    identity=_read(base.HOME/'identity.json')
    builder=joint.ProposalBuilder(catalog,identity)
    for split,documents in [('calibration',calibration),('development',development)]:
        for d in documents:
            path=joint.HOME/'payloads'/split/('paper-'+d['pmid']+'.json')
            if canonical(builder.build(d,split))!=path.read_text(encoding='utf-8'):
                raise ValueError('Joint source/official-ID proposal reconstruction differs')
    all_ids=[]
    profiles=set()
    for model,_ in base.MODELS:
        for split in ('calibration','development'):
            for p in (base.directory(model)/split/'responses').glob('*.json'):
                r=_read(p)
                all_ids.append(r['response_id'])
                profiles.add(r['account_profile_sha256'])
    for phase,total in [('original-calibration',40),('review-calibration',40),('review-development',100)]:
        paths=list((joint.HOME/phase/'responses').glob('*.json'))
        if len(paths)!=total:
            raise ValueError('Incomplete joint response partition')
        for path in paths:
            response=_read(path)
            request=_read(joint.HOME/phase/'requests'/path.name)
            pmid=path.stem.removeprefix('paper-')
            expected=(source_input(docs[pmid]) if phase=='original-calibration' else
                      (joint.HOME/'payloads'/('calibration' if phase=='review-calibration' else 'development')/path.name).read_text(encoding='utf-8'))
            prompt=INSTRUCTIONS if phase=='original-calibration' else joint.PROMPT
            if (response['request_sha256']!=sha(canonical(request)) or response.get('test_only')
                    or response.get('status')!='completed' or response.get('actual_model')!=joint.MODEL
                    or response.get('reasoning_effort')!=joint.EFFORT or request['input']!=expected
                    or request['instructions']!=prompt or request['contract_sha256']!=sha(canonical(contract))):
                raise ValueError('Actual official reviewer response provenance differs')
            all_ids.append(response['response_id'])
            profiles.add(response['account_profile_sha256'])
    if len(all_ids)!=460 or len(set(all_ids))!=460 or len(profiles)!=1:
        raise ValueError('Expected460 distinct real responses from the authorized account')
    selection=joint.select_policy(calibration,contract)
    receipt=_read(joint.HOME/'development-freeze.json')
    if receipt['selection_sha256']!=sha((joint.HOME/'selection.json').read_bytes()):
        raise ValueError('Joint policy changed after freeze')
    predictions={}
    for d in development:
        name='paper-'+d['pmid']+'.json'
        payload=_read(joint.HOME/'payloads/development'/name)
        response=_read(joint.HOME/'review-development/responses'/name)
        saved=_read(joint.HOME/'predictions'/name)
        prediction=joint.project(payload,response['text'],selection['selected'])
        if (saved['response_sha256']!=sha(canonical(response))
                or any(saved[k]!=prediction[k] for k in prediction)):
            raise ValueError('Saved joint prediction reprojection differs')
        for r in prediction['relations']:
            for span in [*r['endpoints'],*r['evidence']]:
                if payload['source'][span['start']:span['end']]!=span['text']:
                    raise ValueError('Accepted original-source span differs')
        predictions[d['pmid']]=prediction
    locator=_read(base.PRIOR/'gpt-6.1-sol-medium-0fd4d4e0/scoring-locator.json')
    gold,_,gold_hash=benchmarks._load_gold(_settings(ROOT/'runtime/chatgpt-inputs'),locator['gold_path'])
    labels=[d for d in gold['documents'] if d['pmid'] in predictions]
    rows=base.score_rows(labels,predictions)
    measured=_metric([r['relation'] for r in rows.values()])
    report=_read(joint.HOME/'comparison.json')
    if measured!=report['metrics'] or rows!=report['per_paper'] or gold_hash!=report['gold_sha256']:
        raise ValueError('Unchanged native v6 score reconstruction differs')
    choice=_read(base.HOME/'overall-selection.json')
    prereg=_read(base.HOME/'overall-selection-preregistration-v2.json')
    selector=ROOT/'scripts/select_chatgpt_tuned_pipeline_v2.py'
    if choice['source_sha256']!=sha(selector.read_bytes()) or prereg['selector_source_sha256']!=sha(selector.read_bytes()):
        raise ValueError('Training-only family selector source differs')
    if (base.HOME/'overall-selection.json').stat().st_mtime>(joint.HOME/'comparison.json').stat().st_mtime:
        raise ValueError('Overall family must be selected before joint development scoring')
    suites=ET.parse(base.HOME/'verification.xml').getroot().findall('testsuite')
    tests={k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
    if tests['tests']<161 or any(tests[k] for k in ('failures','errors','skipped')):
        raise ValueError('The161-test verification is incomplete')
    result={'status':'complete','real_new_completed_responses':460,'unique_response_ids':460,
            'authorized_account_profiles':1,'original_responses_reused':200,
            'joint_payloads_rebuilt_from_source_and_full_official_dictionary':True,
            'joint_native_score_reproduced_offline':True,'accepted_source_offset_mismatches':0,
            'overall_selection_preceded_joint_scoring':True,'tests':tests,
            'default_engine':'heuristic-v1','nutrition_accuracy':'unmeasured','sealed_test_opened':False,
            'base_audit_sha256':sha((base.HOME/'completion-audit.json').read_bytes()),
            'joint_contract_sha256':sha((joint.HOME/'contract.json').read_bytes()),
            'joint_report_sha256':sha((joint.HOME/'comparison.json').read_bytes()),
            'overall_selection_sha256':sha((base.HOME/'overall-selection.json').read_bytes()),
            'auditor_source_sha256':sha(Path(__file__).read_bytes())}
    immutable(base.HOME/'optimized-completion-audit.json',result)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
