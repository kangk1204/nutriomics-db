"""Stored experiment reads: no NLP inference or default-engine changes."""
import json
import re
from .benchmarks import verify_receipt


def experiment_summary(settings):
    path=settings.home/'experiments/v3/summary.json'
    if not path.exists():
        return {'status':'not_started','reports':[],'promotion_eligible':False,'engine':'heuristic-v3'}
    value=json.loads(path.read_bytes())
    if (value.get('schema_version')!='literature-v3-experiment-summary-v1'
            or value.get('engine')!='heuristic-v3' or value.get('promotion_eligible') is not False
            or value.get('sealed_test_reused') is not False
            or not verify_receipt(settings,'public_v3_experiment_summary',value)):
        raise ValueError('개발 비교 결과의 감사 기록과 파일이 일치하지 않습니다.')
    if any(r.get('split')!='development' or r.get('promotion_eligible') is not False for r in value['reports']):
        raise ValueError('개발 자료 외의 결과를 개선 실험으로 표시할 수 없습니다.')
    return value


def nutrition_previews(settings,case_id=None):
    index_path=settings.home/'experiments/v3/nutrition-preview.json'
    if not index_path.exists():
        if case_id is not None:
            raise LookupError('영양 문헌 미리보기가 없습니다.')
        return {'items':[],'nutrition_accuracy':'unmeasured'}
    index=json.loads(index_path.read_bytes())
    if (index.get('split')!='development' or index.get('promotion_eligible') is not False
            or index.get('sealed_test_reused') is not False
            or not verify_receipt(settings,'nutrition_v3_preview_index',index)):
        raise ValueError('영양 문헌 미리보기의 감사 기록이 일치하지 않습니다.')
    if case_id is None:
        return {**index,'items':[{k:v for k,v in item.items() if k!='path'} for item in index['items']]}
    if not re.fullmatch(r'[a-f0-9]{64}',case_id):
        raise ValueError('미리보기 ID 형식을 확인하세요.')
    row=next((item for item in index['items'] if item['id']==case_id),None)
    if row is None:
        raise LookupError('영양 문헌 미리보기가 없습니다.')
    path=(settings.home/row['path']).resolve()
    if not path.is_relative_to((settings.home/'experiments/v3/nutrition').resolve()):
        raise ValueError('미리보기 파일 경로가 저장 영역을 벗어났습니다.')
    value=json.loads(path.read_bytes())
    if (value['id']!=case_id or value['rules_version']!=index['identity']['rules_version']
            or value['split']!='development' or value['source_hash']!=row['source_hash']
            or not verify_receipt(settings,'nutrition_v3_preview_case',value)):
        raise ValueError('미리보기 원문·추출 기록이 변경됐습니다.')
    return value
