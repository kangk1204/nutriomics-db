"""Build an honest final checkpoint, sanitize local export, and document gaps."""

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import connect, coverage
from nutrigenomics.release_rights import SCHEMA, export_verified

ROOT = Path("research/2026-09-30/expansion")


def main():
    db = connect(ROOT / "nutrition_evidence.sqlite3")
    expression_audit = json.loads(
        (ROOT / "reports/connectivity_rights_expression_audit.json").read_text()
    )["expression"]
    with db:
        row = db.execute(
            "SELECT id,metadata_json FROM ng_dataset WHERE accession='GSE27385'"
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        metadata["source_processing_audit"] = expression_audit
        db.execute(
            "UPDATE ng_dataset SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), row["id"]),
        )
        row = db.execute(
            "SELECT id,metadata_json FROM ng_dataset WHERE accession='GSE15284'"
        ).fetchone()
        metadata = json.loads(row["metadata_json"])
        artifact = metadata["measurement_artifact"]["artifact"]
        source = db.execute(
            "SELECT id FROM ng_release WHERE source='GEO-matrix' AND sha256=?",
            (artifact["input_sha256"],),
        ).fetchone()
        if not source:
            raise ValueError("Missing matrix release provenance")
        artifact["release_id"] = source["id"]
        db.execute(
            "UPDATE ng_dataset SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, sort_keys=True), row["id"]),
        )
    report = coverage(db)
    strict_foods = db.execute("""SELECT count(DISTINCT c.subject_id) FROM ng_evidence c
        JOIN ng_entity n ON n.id=c.object_id
        LEFT JOIN ng_mapping m ON m.from_id=n.id AND m.review_status='verified' AND m.relation='exact'
        LEFT JOIN ng_entity molecule ON molecule.id=m.to_id
        WHERE c.layer='composition' AND (n.kind='compound' OR molecule.kind='compound')""").fetchone()[
        0
    ]
    health_subjects = dict(
        db.execute(
            "SELECT n.kind,count(*) FROM ng_evidence e JOIN ng_entity n ON n.id=e.subject_id WHERE e.layer='health_outcome' GROUP BY n.kind"
        )
    )
    report.update(
        completed_at=datetime.now(UTC).isoformat(),
        rights_counts=dict(db.execute("SELECT status,count(*) FROM ng_rights GROUP BY status")),
        mapping_review_status=dict(
            db.execute("SELECT review_status,count(*) FROM ng_mapping GROUP BY review_status")
        ),
        foods=db.execute("SELECT count(*) FROM ng_entity WHERE kind='food'").fetchone()[0],
        verified_full_food_to_health_paths=0,
        strict_food_to_molecule_coverage=strict_foods,
        health_subject_kinds=health_subjects,
        connectivity_basis="Food nutrient identities have four candidate related mappings only. Molecular target/process branch is curated, but no compatible molecule-specific human outcome evidence exists. DASH is whole-diet aggregate evidence.",
        independence_limit="12 distinct study participant groups catalogued, including legacy, aggregate-only, and blocked groups; not 12 fully ingested omics cohorts. Global participant identity cannot be cross-linked.",
        negative_findings="Inherited 484803 CpGs with BH q<.05=0 preserved. No new effect analysis or benefit inference.",
        production_deployed=False,
    )
    datasets = []
    for row in db.execute(
        "SELECT accession,study_id,n_assays,ingest_status,metadata_json FROM ng_dataset"
    ):
        item = dict(row)
        metadata = json.loads(item.pop("metadata_json"))
        item["partial_ingestion"] = metadata.get("partial_ingestion")
        if row["accession"] == "GSE15284":
            artifact = metadata["measurement_artifact"]
            item["measurement"] = {
                k: artifact[k]
                for k in [
                    "n_people",
                    "n_features",
                    "n_samples",
                    "total_values",
                    "missing_values",
                    "reported_zero_values",
                    "processing",
                    "value_unit",
                ]
            }
        if "sample_audit" in metadata:
            item["n_people"] = metadata["sample_audit"]["n_people"]
        datasets.append(item)
    report["datasets"] = datasets
    report["expression_1804275"] = json.loads(
        (ROOT / "reports/connectivity_rights_expression_audit.json").read_text()
    )["expression"]
    report["candidate_priority"] = [
        {
            "rank": 1,
            "accession": "GSE15284",
            "reason": "28 paired participants, two named dietary arms, complete small processed matrix; probe mapping/rights remain unresolved",
        },
        {
            "rank": 2,
            "accession": "GSE323980",
            "reason": "20 people, 40 source assays; two processed files captured. Arm/time codebook, CSV aliases, normalization and two mixed/missing feature rows block comparison",
        },
        {
            "rank": 3,
            "accession": "GSE46761",
            "reason": "Microbial transcriptome, repeated people across diets; 1 of deposited assays captured. Quantitative scaling and microbial feature mapping unresolved",
        },
    ]
    db.close()
    # Preserve earlier experimental export but mark it as unsuitable for sharing.
    for suffix in ["sqlite3", "rights.json"]:
        old = ROOT / f"shareable_food_composition.{suffix}"
        quarantined = ROOT / f"QUARANTINED_unvalidated_export.{suffix}"
        if old.exists() and not quarantined.exists():
            old.replace(quarantined)
    export = ROOT / "shareable_food_composition_v3.sqlite3"
    if not export.exists():
        report["export"] = export_verified(ROOT / "nutrition_evidence.sqlite3", export)
    else:
        report["export"] = json.loads(export.with_suffix(".rights.json").read_text())
    report["artifact_hashes"] = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [ROOT / "nutrition_evidence.sqlite3", export]
    }
    (ROOT / "reports/final_continuation_coverage.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    (ROOT / "migrations/002_rights_up.sql").write_text(
        SCHEMA
        + "\nINSERT OR IGNORE INTO ng_migration VALUES ('nutrition-rights-v1',datetime('now'));\n",
        encoding="utf-8",
    )
    (ROOT / "migrations/002_rights_down.sql").write_text(
        "BEGIN;\nDROP TABLE IF EXISTS ng_rights;\nDELETE FROM ng_migration WHERE version='nutrition-rights-v1';\nCOMMIT;\n",
        encoding="utf-8",
    )
    text = f"""NutriGenomics 공개 데이터 확장 후속 검증 — 2026-10-01

로컬 연구 DB: {report["tables"]["ng_evidence"]}개 근거, {report["tables"]["ng_mapping"]}개 매핑.
식품 성분 332개, 분자 식별 근거 4개, 분자 역할 2개, GO 생물학적 과정 주석 10개, DASH 전체 식이 혈압 대비 4개.
GO 주석은 실험 근거 코드와 PubMed ID를 보존하며, 경로 지도나 임상 건강 효과로 해석하지 않습니다.

추가 후보 연구 3개를 검증했습니다.
1. FISHGASTRO GSE15284: 28명, 56 전후 검체. 전체 처리 행렬 수집·Parquet 변환 완료. GCRMA 처리이며 원시 CEL은 받지 않았습니다.
2. GSE323980: 20명, 40 검체 메타데이터. 처리 파일 2개만 수집했습니다. 파일당 62,705행 중 2행의 식별자와 값이 불명확/누락입니다. 누락과 0을 구분해 보존합니다. GEO D01A/B와 CSV AMa68A/B, 식이군 및 시점 코드 해석이 미확인이라 효과 분석은 하지 않았습니다.
3. GSE46761: 사람 장내 미생물 전사체. 반복 참가자 메타데이터와 처리 파일 1개만 수집했습니다. 121,021개 미생물 특징, 두 값 열을 보존합니다. 소수 count 값의 스케일은 미확인입니다.
12개 연구 참가자 그룹은 목록상의 수입니다. 기존 연구·논문 집계·접근 제한 그룹을 포함하며, 완전히 수집한 독립 오믹스 코호트 12개를 뜻하지 않습니다.

기존 1,804,275개 발현값은 GSE27385 PBMC의 54,675개 탐침 × 33 배열, 11명 × 세 처치입니다. 원본은 Bioconductor RMA 처리값이며 로컬에서는 형식만 변환했습니다. 로그 밑·정확한 단위는 원본 헤더에 명시되지 않았습니다.

분자 연결은 UniProt PLOD1의 아스코르베이트 보조인자 역할, LARS1의 류신 기질 역할을 분리합니다. ChEBI와 PubChem InChIKey를 검사했고 산과 음이온은 정확 매핑으로 합치지 않았습니다. 단백질과 유전자는 다른 종류의 개체이며 관련 매핑으로 보존합니다.
엄격하게 검증된 식품→분자 연결 범위는 0이며, 식품→건강 결과 전체 연결도 0입니다. 식품 영양성분→분자 후보 매핑 4개는 추가 검토가 필요합니다. 분자 작용으로 식품의 건강상 이익을 추론하지 않습니다.

재배포: USDA FoodData Central의 공식 CC0/공공영역 안내를 캡처·해시 기록했습니다. 확인된 자료 3개와 성분 근거 332개만 shareable_food_composition_v3.sqlite3에 포함했습니다. 그 외 {report["rights_counts"].get("quarantined", 0)}개 릴리스는 재배포 검증 전 격리합니다. 제외 자료가 SQLite 빈 페이지에 남지 않도록 삭제 정리·VACUUM을 적용했습니다. 산출물은 로컬에만 있고 게시·운영 배포하지 않았습니다.

남은 실제 제한: GEO 제출자 자료와 UniProt/ChEBI 등 출처별 재배포 조건 검증, 후보 연구 코드북, GPL 탐침/미생물 특징 매핑, 동일 연구의 개별 건강 결과 연결. NHANES·Reactome·ChEMBL 추가 약정은 승인 없이 수락하지 않았습니다. 이러한 약정의 승인 여부만 사용자 결정이 필요한 접근 경로입니다. 나머지 검증은 별도 약정 없이 계속 진행할 수 있습니다.
기존 484,803 CpG에서 BH q<0.05=0이라는 음성 결과는 보존했습니다. GPU 사용·다른 프로젝트 변경·임상 권고는 없습니다.

재현: scripts/import_continuation.py → scripts/import_candidate_subset.py → scripts/finalize_expansion_continuation.py. 재수집 대신 저장된 체크섬 스냅샷을 사용합니다. 원본 DB 백업 및 rights up/down SQL을 보존했습니다.
상세: reports/final_continuation_coverage.json, reports/candidate_subset_validation.json.
"""
    (ROOT / "NutriGenomics_public_expansion_2026-10-01.txt").write_text(text, encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in ["datasets", "expression_1804275"]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
