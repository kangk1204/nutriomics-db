from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.download_kfind_standard import download_kfind_standard


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, pages: dict[int, list[dict[str, str]]]) -> None:
        self.pages = pages
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, *, params: object = None, timeout: int = 0) -> FakeResponse:
        del timeout
        if url.endswith("standard.do"):
            return FakeResponse({"html": "public standard page"})
        if url.endswith("columList.json"):
            return FakeResponse(
                {
                    "totalCount": "3",
                    "fileName": "K-FIND test fixture",
                    "tableVO": {
                        "svcTableNm": "food_table",
                        "colNmList": ["FOOD_CD", "FOOD_NM", "NUT_CON_SRTR_QUA"],
                    },
                }
            )
        query = dict(params or [])
        page = int(query["page"])
        return FakeResponse(self.pages[page])

    def close(self) -> None:
        self.closed = True


def test_kfind_download_saves_original_pages_and_hashes(tmp_path: Path) -> None:
    session = FakeSession(
        {
            1: [
                {"FOOD_CD": "F001", "FOOD_NM": "밥", "NUT_CON_SRTR_QUA": "100g"},
                {"FOOD_CD": "F002", "FOOD_NM": "국", "NUT_CON_SRTR_QUA": "100g"},
            ],
            2: [{"FOOD_CD": "F003", "FOOD_NM": "나물", "NUT_CON_SRTR_QUA": "100g"}],
        }
    )
    output = tmp_path / "release"
    manifest = download_kfind_standard(output, session=session, page_size=2)
    assert manifest["total_count"] == 3
    assert manifest["page_record_counts"] == [2, 1]
    assert json.loads((output / "page-0001.json").read_text(encoding="utf-8"))[0]["FOOD_NM"] == "밥"
    assert "page-0001.json" in manifest["sha256"]
    assert session.closed is False
    with pytest.raises(FileExistsError):
        download_kfind_standard(output, session=session, page_size=2)


def test_kfind_download_refuses_incomplete_page(tmp_path: Path) -> None:
    session = FakeSession({1: [{"FOOD_CD": "F001", "FOOD_NM": "밥", "NUT_CON_SRTR_QUA": "100g"}]})
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="expected 2"):
        download_kfind_standard(output, session=session, page_size=2)
    assert not output.exists()
    assert not list(tmp_path.glob(".release.partial-*"))


def test_kfind_download_preserves_duplicate_source_codes(tmp_path: Path) -> None:
    duplicate = {"FOOD_CD": "F001", "FOOD_NM": "밥", "NUT_CON_SRTR_QUA": "100g"}
    session = FakeSession({1: [duplicate, duplicate], 2: [{**duplicate, "FOOD_CD": "F003"}]})
    output = tmp_path / "release"
    manifest = download_kfind_standard(output, session=session, page_size=2)
    assert manifest["total_count"] == 3
    assert manifest["unique_food_code_count"] == 2
    assert manifest["duplicate_food_code_excess_count"] == 1
    assert json.loads((output / "page-0001.json").read_text(encoding="utf-8"))[0] == duplicate
