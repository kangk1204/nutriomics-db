# 유전자 검색과 사람 중재 결과

검색창에 유전자 기호(APOE), 정식 영문명(apolipoprotein E), 검색용 한글명(아포지단백 E), 별칭, HGNC·NCBI Gene·Ensembl ID를 입력할 수 있습니다. 통합 검색과 유전자 분류에서 모두 사용할 수 있습니다.

1. 검색 결과에서 원본 ID를 확인하고 유전자를 선택합니다.
2. **근거 비교**에서 해당 유전자를 측정한 중재 대비를 선택합니다.
3. 조직·기간·대상자·검수 상태와 원문을 확인합니다.
4. **선택 유전자의 발현 결과**를 누른 후 **선택 유전자 결과 조회**를 실행합니다. 원본 측정 ID로 필터링합니다. 전체 대비를 보려면 ‘선택 유전자만 조회’를 해제합니다.

검색어 또는 항목 유형을 바꾸면 이전 선택의 근거·계산·오믹스 결과와 내보내기를 해제합니다. 새 항목을 직접 선택해야 결과가 표시됩니다. 오타를 다른 유전자나 음식으로 자동 대체하지 않습니다. 검색 실패와 정상적인 결과 없음은 별도 상태로 표시합니다. 이름 전체의 일치와 일부 문자열 일치도 구분합니다.

같은 유전자의 Ensembl 기록과 NCBI Gene 기록은 원본 ID를 유지합니다. 이를 독립 유전자나 독립 연구로 더하지 않습니다. 공통 별칭(NRF2 등)은 여러 후보를 보여 주므로 정식 기호와 ID를 확인해야 합니다.

영문명·별칭은 HGNC 공식 자료를 사용합니다. 한글명 100개는 검색을 돕기 위해 작성한 용어이며 공식 한글명이나 전문가 승인을 뜻하지 않습니다. 현재 공식 ID에 대응하지 않는 원본 기록은 이름만으로 다른 유전자에 합치지 않습니다.

중재 연결은 분석 코드에 명시된 측정 ID 체계와 실제 결과를 대조합니다. CpG 근접 주석이나 조절 예측을 발현 결과로 바꾸지 않습니다. 비유의 결과도 포함합니다. ‘측정됨’은 발현 변화·건강상 이득·인과성이 검증됐다는 뜻이 아닙니다. 약물 비교는 식이중재로 분류하지 않습니다.

## 검색 자료 재구축

`scripts/build_gene_names.py`는 원본 그래프나 오믹스 값을 수정하지 않고 별도 SQLite 색인을 만듭니다. 입력은 HGNC TSV, 원본 유전자 목록, `config/gene_names_ko/curated.json`, `config/gene_feature_namespaces.json`입니다. 출력 파일을 덮어쓰지 않으며 새 경로를 지정해야 합니다.

```console
python -m scripts.export_gene_inventory --release /path/to/release --output gene-inventory.json
python -m scripts.build_gene_names --hgnc hgnc_complete_set.txt --inventory gene-inventory.json --korean config/gene_names_ko/curated.json --contracts config/gene_feature_namespaces.json --output gene-names.sqlite3
```

`NUTRIGENOMICS_GENE_NAMES_FILE`에 출력 경로를 지정합니다. 시작 검사에서 데이터 release와 DB 해시가 일치해야 합니다. 분석 코드가 달라졌으면 ID 체계와 코드 해시를 재검토해야 합니다. 수집 성공만으로 검수 승인 상태를 만들지 않습니다.

출처: [HGNC 다운로드](https://www.genenames.org/download/statistics-and-files/), [HGNC 이용조건(CC0)](https://www.genenames.org/about/license/).

<details><summary>English</summary>

Search by approved symbol, approved English name, alias, authored Korean search term, HGNC ID, NCBI Gene ID or Ensembl ID. Select a gene, open a measured intervention contrast, inspect its conditions and source, then query the exact native expression feature. Shared aliases retain multiple candidates. Original graph IDs remain separate records.

Korean terms are authored search aids, not official nomenclature or expert approvals. Intervention links require a matching analysis-specific identifier namespace and an actual quantitative result. Nonsignificant results remain available. Measurement, regulation, mediation and clinical benefit are distinct claims. Rebuild the immutable catalog with the command above; its manifest binds the HGNC source, inventory, terminology, producer-code hashes and dataset release.

</details>
