"""Opt-in internal retrieval of a hash-pinned pathway method correction.

The ordinary historical API is unchanged. Corrected requests return the actual
new five-part key and never confer scientific/publication approval.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from fastapi import HTTPException

from .explorer import _json, _public
from .explorer_context import configured_path
from .pathway_selection import PathwaySelection


def correction_metadata():
    if not os.environ.get('NUTRIOMICS_PATHWAY_SELECTION'):
        return None
    if os.environ.get('NUTRIOMICS_CORRECTION_ACCESS') != 'internal_review':
        return {'status': 'not_enabled_for_this_service', 'scientific_approval': False}
    try:
        expected = os.environ.get('NUTRIOMICS_PATHWAY_SELECTION_SHA256', '')
        reader = PathwaySelection(configured_path('NUTRIOMICS_PATHWAY_SELECTION', ''),
            configured_path('NUTRIOMICS_PATHWAY_ARTIFACT_ROOT', ''), expected_sha256=expected)
        return dict(status='internal_review_candidate', overlay_sha256=expected,
            selected_pathway_rows=reader.overlay['rows'],
            original_core_rows_modified=0, additional_people=0,
            scientific_approval=False, expert_review_status='pending', public_allowed=False,
            historical_checkpoint_recovered=False,
            runs=[{k:r[k] for k in ('parent_analysis_id','parent_analysis_version',
                'analysis_version','rows','primary_q_family','q_threshold_changes')}
                for r in reader.overlay['selectors']],
            count_definition='Version-selected replacement of pathway outputs, not additive independent results or people.')
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(503, '경로 교정판 metadata의 무결성 확인에 실패했습니다.') from error


def corrected_page(db, *, release, series, contrast, analysis_version, feature_id,
                   page, page_size, cursor):
    if series != 'GSE56960':
        return None
    if os.environ.get('NUTRIOMICS_CORRECTION_ACCESS') != 'internal_review':
        raise HTTPException(409, '경로 교정판은 내부 검수 설정에서만 조회할 수 있습니다.')
    path = configured_path('NUTRIOMICS_PATHWAY_SELECTION', '')
    root = configured_path('NUTRIOMICS_PATHWAY_ARTIFACT_ROOT', '')
    expected = os.environ.get('NUTRIOMICS_PATHWAY_SELECTION_SHA256', '')
    try:
        reader = PathwaySelection(path, root, expected_sha256=expected)
        request = dict(series_accession=series, feature_type='pathway',
                       contrast=contrast, analysis_version=analysis_version)
        resolved = reader.resolve(request, purpose='internal_technical')
        if resolved['artifact'] is None:
            return None
        parent = next(r for r in reader.overlay['selectors']
                      if r['analysis_version'] == resolved['selected']['analysis_version'])
        registered = any(
            (c.get('series_accession'), c.get('contrast'), c.get('analysis_version'))
            == (series, contrast, parent['parent_analysis_version'])
            for row in db.execute("SELECT metadata_json FROM science_record WHERE kind='contrast'")
            for c in [_json(row[0])]
        )
        if not registered:
            raise HTTPException(404, '부모 분석 키가 현재 release에 없습니다.')
        original = db.execute("SELECT metadata_json FROM science_record WHERE kind='analysis_run' AND id=?",
                              (str(parent['parent_analysis_id']),)).fetchone()
        if original is None or hashlib.sha256(json.dumps(_json(original[0]), ensure_ascii=False,
                sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest() != parent['parent_record_sha256']:
            raise ValueError('Parent analysis record changed; rebind and review the correction')
        identity = [release, expected, resolved['selected'], feature_id]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        offset = (page - 1) * page_size
        if cursor:
            try:
                value = json.loads(base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4)))
                if value['key'] != key:
                    raise HTTPException(409, 'Cursor의 교정판·release·조회 조건이 변경됐습니다.')
                offset = value['offset']
                if type(offset) is not int or not 0 <= offset <= 100000000:
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                raise HTTPException(422, '올바르지 않은 cursor') from error
        table = reader.read(request, purpose='internal_technical', feature_id=feature_id)
        table = table.sort_by([('feature_id', 'ascending')])
        total = table.num_rows
        items = table.slice(offset, page_size).to_pylist()
        for row in items:
            row['metadata'] = _public(_json(row.pop('metadata_json', None)))
            row['analysis_key'] = [row[k] for k in
                ('series_accession','feature_type','feature_id','contrast','analysis_version')]
            row['expert_review_status'] = 'pending'
        from .feature_taxonomy import annotate_effect_record
        items = [annotate_effect_record(row) for row in items]
        token = base64.urlsafe_b64encode(json.dumps(dict(key=key, offset=offset+len(items))).encode()).decode().rstrip('=')
        return dict(release_id=release, status='available', items=_public(items), total=total,
                    feature_type='pathway', requested_feature_type='pathway',
                    available_feature_types=['pathway'], page=page, page_size=page_size,
                    next_cursor=token if items and offset+len(items)<total else None,
                    method_correction=dict(overlay_sha256=expected, requested=request,
                        selected=resolved['selected'], parent_analysis_id=parent['parent_analysis_id'],
                        parent_record_sha256=parent['parent_record_sha256'],
                        artifact_sha256=resolved['artifact']['sha256'],
                        primary_q_family=parent['primary_q_family'],
                        historical_reproducibility_resolved=False,
                        current_checkpoint_reproducible=True, expert_review_status='pending',
                        public_allowed=False, q_threshold_changes_in_parent=parent['q_threshold_changes']),
                    note='정확한 입력으로 재현한 새 경로 버전입니다. 과거 값 복구·전문가 승인·건강 효능 판정이 아닙니다. NES SE와 Monte Carlo log2err를 구분합니다.')
    except HTTPException:
        raise
    except (OSError, ValueError, KeyError, PermissionError) as error:
        # A bad/missing correction never becomes zero or historical fallback.
        raise HTTPException(503, '경로 교정판의 파일·해시·분석 키 확인에 실패했습니다.') from error
