# DB 구조도 자료

[구조 설명](../../DB_STRUCTURE.ko.md) · [English explanation](../../DB_STRUCTURE.md)

보고서용 [PNG](보고서용_DB_구조도.png), 벡터 [SVG](보고서용_DB_구조도.svg)와
[PDF](보고서용_DB_구조도.pdf)를 같은 코드로 생성합니다. PNG는 300 dpi입니다.
`nodes.csv`와 `edges.csv`에는 구성 요소와 연결의 의미를 기록했습니다.
`structure-source.json`은 수치 기준과 원본 metadata의 SHA-256을 기록합니다.
전체 DB나 비공개 서버 설정은 포함하지 않습니다.

저장소 루트에서 다음 명령으로 다시 그릴 수 있습니다.

```sh
uv sync --frozen --extra figures
uv run python docs/figures/db-structure/make_korean_structure.py
```

Windows에서는 맑은 고딕을 사용합니다. Linux에서는 Noto Sans CJK KR 또는
NanumGothic 등 한글 글꼴이 필요합니다. 별도 글꼴 파일은
`NUTRIOMICS_FIGURE_FONT` 환경변수로 지정할 수 있습니다. 글꼴·라이브러리가
다르면 그림의 바이트는 달라질 수 있으며 실행 환경은 `provenance.json`에 남깁니다.

선은 자료 조회의 관계입니다. 식품의 효능·인과성이나 모든 식품의 완전한 연결을
입증하지 않습니다. 구조 검사는 전문가의 과학적 수록 판정을 대신하지 않습니다.
