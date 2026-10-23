# 한글 식품 검색과 원본 기록

공개 후보 release `nutriomics-composed-candidate-20261022-v3`의 식품 5,794개에 한글 표시명과 검색 별칭을 연결했다. 원본 영문명, 식품 코드, 조리상태, 성분값은 그대로 유지한다. 한글명은 검색·표시를 위한 자체 작성 용어로 구성했으며 공식 번역이나 전문가 승인으로 표시하지 않는다. 상표·비타민 표기 등 91개 기록에는 원래 영문 일부를 남겼다.

## 화면에서 사용하기

1. 항목 유형에서 **음식**을 고른다.
2. `김치`, `계란`, `달걀`, `닭가슴살 생것`, `무가당 아몬드`, `통밀빵`, `귤`처럼 입력한다. 띄어쓰기가 다른 표현도 검색한다.
3. 한글명 아래 원본 영문명·식품 코드를 확인해 기록을 선택한다.
4. g 단위 섭취량을 정하고 **성분 섭취량 계산**을 누른다.
5. 연구 근거에서는 시험 제품·용량·기간·조직·검수 상태를 별도로 확인한다. 공통 성분으로 연결된 복합 중재는 선택한 음식 자체의 효과가 아니다.

`생것`, `삶은`, `무가당`, `가당`, `저지방`, `무지방` 등은 원본에 해당 조건이 명시된 후보를 찾는다. `배`와 배추·배스, `무`와 무가당, `전란`과 흰자, 조리된 밥과 생쌀을 구분한다. 생재료로 조리한 식품을 생식품으로 분류하지 않는다. 출처에 없는 된장찌개·김치찌개를 김치나 미소국으로 대신하지 않는다.

## 관리 파일

| 파일 | 역할 |
|---|---|
| `config/food_names_ko/lexicon.txt` | 문맥을 고려한 영문 구절–한글 용어 |
| `config/food_names_ko/synonyms.json` | 방향이 있는 검색 별칭. 식품·성분 동일성 관계가 아님 |
| `config/food_names_ko/catalog.json` | 원본 ID·영문명 해시에 묶인 기록별 표시명·별칭 |
| `config/food_names_ko/catalog.manifest.json` | 파일 해시·건수·남겨 둔 원문 표기·검수 상태 |
| `nutrigenomics/food_names.py` | 읽기 전용 이름 연결, 원본 이름 재확인, 검색·캐시 처리 |

서비스가 사용하는 이름표는 `NUTRIGENOMICS_FOOD_NAMES_FILE`로 선택할 수 있다. 기본값은 위 catalog다. 이름표나 manifest가 손상되면 503 오류를 반환하며 검색 결과 0건으로 감추지 않는다. 파일 교체 시 캐시를 무효화한다. 이름표가 없는 기존 설치에서는 기존 영문 검색을 유지한다.

API의 기존 `label`은 원문이다. `display_name_ko`, `search_aliases_ko`, `name_translation`을 추가했다. `/api/v1/explorer/meta`의 `food_names_ko`에서 이름표 버전과 해시를 확인할 수 있다. 독립 HTML은 snapshot에 포함된 한글명·별칭만 검색한다. 서버의 전체 식품을 담았다는 의미는 아니다.

## 이름표 다시 만들기

입력 inventory JSON은 `release_id`와 `items`를 포함한다. 각 item은 원본 `id`, `label`, `metadata.food_category`를 보존한다. 해당 release의 음식 조회 API를 모든 페이지에 걸쳐 수집하고 누락·중복·release 변경을 확인한 자료를 사용한다.

```text
python -m scripts.build_food_names_ko --inventory food-inventory.json --lexicon config/food_names_ko/lexicon.txt --synonyms config/food_names_ko/synonyms.json --output staging/food_names_ko/catalog.json
python -m pytest tests/test_food_names_ko.py tests/test_food_search_aliases.py tests/test_explorer_api.py tests/test_explorer_scientific_api.py tests/test_explorer_performance.py tests/test_public_meal_batch.py tests/test_composition_policy.py -q
```

현재 운영 중인 이름표를 직접 편집하지 않는다. 새 staging 디렉터리에서 생성·검사하고 원본 ID·이름·release와 일치하는지 확인한 뒤 catalog와 manifest를 함께 교체한다. 데이터 release, 이름표 해시, 코드 버전과 검증 결과를 같이 보존한다. 영양성분이나 연구 근거를 새 이름표로 수정하지 않는다.

<details><summary>English</summary>

The optional Korean display/search catalog covers 5,794 source-bound public food records. Original labels, food IDs, preparation distinctions and nutrient values remain unchanged. Names are composed from authored terminology and directed aliases, not official USDA translations or expert-reviewed food identities. Record bindings, catalog checksums and versioned metadata prevent silent reuse against changed source names. Search additions do not establish equivalence to intervention products or evidence of health effects.

</details>
