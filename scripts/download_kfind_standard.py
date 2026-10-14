from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

DATASETS = {
    "15100065": "전국통합식품영양성분정보(원재료성식품)표준데이터",
    "15100070": "전국통합식품영양성분정보(음식)표준데이터",
}
COLUMN_URL = "https://www.data.go.kr/download/columList.json"
RECORD_URL = "https://www.data.go.kr/download/standard.json"
DEFAULT_PAGE_SIZE = 10_000


def _response_json(response: Any, endpoint: str) -> tuple[dict[str, Any] | list[Any], bytes]:
    response.raise_for_status()
    body = response.content
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"K-FIND returned invalid JSON from {endpoint}") from error
    if isinstance(payload, dict) and payload.get("RESULT", {}).get("CODE"):
        result = payload["RESULT"]
        raise RuntimeError(f"K-FIND error {result['CODE']}: {result.get('MSG', 'unknown error')}")
    if not isinstance(payload, (dict, list)):
        raise TypeError(f"K-FIND returned an unexpected JSON root from {endpoint}")
    return payload, body


def download_kfind_standard(
    output_dir: Path,
    *,
    dataset_id: str = "15100070",
    dataset_name: str | None = None,
    session: Any | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Save the public K-FIND food-standard download, unchanged and with hashes."""
    if page_size < 1 or page_size > DEFAULT_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {DEFAULT_PAGE_SIZE}")
    if dataset_id not in DATASETS:
        raise ValueError(f"Unsupported K-FIND dataset ID: {dataset_id}")
    resolved_dataset_name = dataset_name or DATASETS[dataset_id]
    source_page = f"https://www.data.go.kr/data/{dataset_id}/standard.do"
    final_dir = output_dir.expanduser().resolve()
    if final_dir.exists():
        raise FileExistsError(f"Refusing to replace an existing release directory: {final_dir}")
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{final_dir.name}.partial-", dir=final_dir.parent))
    client = session or requests.Session()
    client.headers.update(
        {
            "User-Agent": "Nutrigenomics-Research-DB/0.1 (non-commercial academic research)",
            "Referer": source_page,
            "X-Requested-With": "XMLHttpRequest",
        }
    )

    try:
        # The public download endpoints use a page session cookie. No account or API key is sent.
        page_response = client.get(source_page, timeout=60)
        page_response.raise_for_status()
        header_response = client.get(COLUMN_URL, params={"pk": dataset_id, "ext": "JSON"}, timeout=60)
        header, header_bytes = _response_json(header_response, COLUMN_URL)
        if not isinstance(header, dict):
            raise TypeError("K-FIND column metadata must be a JSON object")
        columns = header.get("tableVO", {}).get("colNmList")
        table_name = header.get("tableVO", {}).get("svcTableNm")
        try:
            total_count = int(header["totalCount"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("K-FIND metadata has no valid totalCount") from error
        if not columns or not table_name or total_count < 1:
            raise ValueError("K-FIND metadata is missing columns, table name, or records")
        if not {"FOOD_CD", "FOOD_NM", "NUT_CON_SRTR_QUA"}.issubset(columns):
            raise ValueError("K-FIND food-standard metadata lacks required food or basis columns")

        (staging_dir / "columns.json").write_bytes(header_bytes)
        page_count = math.ceil(total_count / page_size)
        page_records: list[int] = []
        food_codes: set[str] = set()
        duplicate_code_excess: dict[str, int] = {}
        reference_dates: list[str] = []
        file_hashes: dict[str, str] = {
            "columns.json": hashlib.sha256(header_bytes).hexdigest(),
        }
        for page in range(1, page_count + 1):
            params = [
                ("publicDataPk", dataset_id),
                *(("colNmList", str(column)) for column in columns),
                ("totalCount", str(total_count)),
                ("svcTableNm", str(table_name)),
                ("perPage", str(page_size)),
                ("page", str(page)),
            ]
            response = client.get(RECORD_URL, params=params, timeout=120)
            payload, body = _response_json(response, RECORD_URL)
            if not isinstance(payload, list):
                raise TypeError(f"K-FIND page {page} is not a JSON record array")
            expected_count = min(page_size, total_count - (page - 1) * page_size)
            if len(payload) != expected_count:
                raise ValueError(
                    f"K-FIND page {page} returned {len(payload)} records; expected {expected_count}"
                )
            for row in payload:
                if not isinstance(row, dict) or not row.get("FOOD_CD") or not row.get("FOOD_NM"):
                    raise ValueError(f"K-FIND page {page} contains a record without food identity")
                food_code = str(row["FOOD_CD"])
                if food_code in food_codes:
                    duplicate_code_excess[food_code] = duplicate_code_excess.get(food_code, 0) + 1
                food_codes.add(food_code)
                reference_date = row.get("CRTR_YMD")
                if reference_date:
                    reference_dates.append(str(reference_date))
            filename = f"page-{page:04d}.json"
            (staging_dir / filename).write_bytes(body)
            file_hashes[filename] = hashlib.sha256(body).hexdigest()
            page_records.append(len(payload))

        if sum(page_records) != total_count:
            raise ValueError("K-FIND record total check failed")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        manifest = {
            "dataset_id": dataset_id,
            "dataset_name": resolved_dataset_name,
            "source_page": source_page,
            "metadata_endpoint": COLUMN_URL,
            "records_endpoint": RECORD_URL,
            "provider": "Ministry of Food and Drug Safety / DATA.GO.KR",
            "downloaded_utc": timestamp,
            "total_count": total_count,
            "page_size": page_size,
            "page_count": page_count,
            "page_record_counts": page_records,
            "unique_food_code_count": len(food_codes),
            "duplicate_food_code_excess_count": sum(duplicate_code_excess.values()),
            "duplicate_food_code_examples": dict(list(duplicate_code_excess.items())[:20]),
            "provider_reference_date_min": min(reference_dates) if reference_dates else None,
            "provider_reference_date_max": max(reference_dates) if reference_dates else None,
            "license_status": "The standard-data listing did not state a license in its visible metadata; retain attribution and restrict redistribution pending confirmation.",
            "raw_format": "Original JSON response bytes saved per page",
            "sha256": file_hashes,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (staging_dir / "manifest.json").write_bytes(manifest_bytes)
        if final_dir.exists():
            raise FileExistsError(f"Refusing to replace an existing release directory: {final_dir}")
        os.replace(staging_dir, final_dir)
        return {**manifest, "output_dir": str(final_dir)}
    except BaseException:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise
    finally:
        if session is None:
            client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download official K-FIND food-standard JSON pages")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--dataset-id", choices=sorted(DATASETS), default="15100070")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    args = parser.parse_args()
    print(
        json.dumps(
            download_kfind_standard(
                args.output_dir, dataset_id=args.dataset_id, page_size=args.page_size
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
