# NutriOmics Explorer

로그인 없는 한국어 연구 워크벤치. React/TypeScript, Cytoscape.js+Dagre, TanStack Table v8을 사용합니다.

```powershell
pnpm install --frozen-lockfile
pnpm run build
```

Vite는 `/explorer/` 기준으로 빌드됩니다. 기존 FastAPI가 `dist`의 `index.html` 및 `assets`를 제공하며 `/api/v1/explorer/*`와 동일 origin을 사용합니다. 개발 서버의 API 프록시는 `127.0.0.1:18000`입니다.

`dist/offline-runtime.js`는 React·Cytoscape·Dagre를 포함한 단일 IIFE이며 `dist/offline-runtime.css`와 함께 HTML 안에 내장됩니다. 실행 전 `window.__NUTRIOMICS_SNAPSHOT__`에 선택된 실제 노드·관계·출처와 `offline:true`를 넣습니다. 오프라인 실행에서는 `fetch`를 호출하지 않습니다. 검색은 내장된 snapshot으로 한정됩니다.

노드 선택은 표의 유형과 상세 패널을 동기화합니다. 관계는 동일 endpoint와 tier를 묶어 표시하고, 상세 패널에서 개별 기록을 선택합니다. 관계 유형·검수·종·맥락·원본 출처를 표시하며 개인 건강 예측을 생성하지 않습니다. 음식 계산은 검증된 `metadata.core_food_id` 또는 `metadata.food_id`가 있고 서버에 연결된 경우에만 기존 식사 API를 호출합니다.

중앙 작업 영역은 **사람 중재 근거**와 **연결 가설**로 나뉩니다. 기본 화면에서 실제 사람 연구의 노출·대비·출처 위치와 통계값을 확인하고, 그래프의 조합 경로는 별도 탭에서 탐색합니다. 성분·분자 대응, 연구 근거 종류, 입력 조건의 일치 여부는 각각 표시하며 이를 종합 효능 점수로 바꾸지 않습니다.

`POST /api/v1/explorer/match`에 탐색 기준 `seed_id`, 음식에 한정한 `intake_g`, 선택적 `reference_context`(조직·반응 시간·반복 기간·빈도·대상군)를 보냅니다. 입력하지 않은 조건은 미확인입니다. 성분·식단·중재 기록을 선택한 경우 음식 g를 성분 투여량으로 해석하지 않습니다. 음식 탐색 기준은 그래프의 유전자 선택과 독립적으로 유지되며, 유전자 선택은 결과를 좁히는 별도 조건입니다. 요청 취소와 요청 버전 확인으로 과거 조건의 응답이 현재 결과를 덮어쓰지 않습니다.

같은 중재의 시점별 조건은 대비마다 표시합니다. 원본 시료 수와 독립 대상자 수를 구분하고, 원본 FDR 검정군·분석 버전은 펼쳐 확인할 수 있습니다. 기본 결과 미리보기는 특성 이름 순서이며 유의성 순위가 아닙니다. 임상지표가 연결되지 않으면 정상적인 자료 부족 결과로 표시합니다. 출처 대조 상태와 전문가 검수 상태도 별개입니다.

공유 측정 성분만으로 대응한 연구는 별도 접힌 그룹에 넣고 전체 중재 기록 수를 표시합니다. 기본 음식 대응 기록을 먼저 보여주며 효능 순위로 분류하지 않습니다. 네이티브 `details/summary`를 사용하고, 닫힌 대비·원문·통계 기록의 본문은 DOM에서 제외했다가 펼칠 때 렌더링합니다. 시료 범위의 출처별 값이 충돌하면 해당 수치를 미확인으로 유지하고 출처별 기록을 확인하도록 안내합니다.

내보내기에 `filters.scientific_context`를 넣으면 백엔드는 해당 탐색 기준의 저장 결과를 `node_details[seed_id].scientific_matches`에 포함합니다. 독립 HTML은 이 결과와 당시 조건을 표시합니다. 새 조건이나 다른 유전자 선택에 대한 과학적 대응을 재계산하지 않습니다. 이용조건에 따라 통계 결과가 빠지거나 제한된 관계가 출처 참조로 대체될 수 있습니다.

실제 내보낸 HTML에 대한 선택·검색·모바일 탭·네트워크 차단 smoke check:

```powershell
$env:EXPLORER_PLAYWRIGHT='C:/Users/kangk/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs'
$env:EXPLORER_BROWSER='C:/Program Files/Google/Chrome/Application/chrome.exe'
node scripts/smoke-offline.mjs path/to/exported.html
```

테스트 스크립트는 HTML 또는 실제 source snapshot JSON을 받습니다. 출력 screenshot와 검사 기록은 `artifacts/`에 보관합니다. 테스트용 가상 근거를 운영 데이터에 넣지 않습니다.
