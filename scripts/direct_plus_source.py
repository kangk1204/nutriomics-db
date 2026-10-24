"""Strict public BioStudies file resolution; no inference from obsolete file lists."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

STUDY = "E-MTAB-12527"
API = f"https://www.ebi.ac.uk/biostudies/api/v1/studies/{STUDY}"
SUCCESS = {"downloaded", "cached_verified"}


def safe_file_path(value: str) -> str:
    """Reject traversal, URL/drive syntax, escaped traversal and basename collisions."""
    if not isinstance(value, str) or not value or re.search(r"[\\:%?#\x00-\x1f]", value):
        raise ValueError(f"Unsafe source file path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError(f"Unsafe source file path: {value!r}")
    return str(path)


def source_base(info: dict) -> str:
    base = info.get("httpLink", "").rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or parsed.hostname != "ftp.ebi.ac.uk"
            or parsed.query or parsed.fragment or parsed.username
            or not parsed.path.startswith("/biostudies/")
            or not parsed.path.endswith("/" + STUDY)):
        raise ValueError("Official info has an unexpected HTTPS source location")
    if info.get("isPublic") is not True:
        raise ValueError("Study is not explicitly public")
    return base


def resolve_files(info: dict, get_page, page_size: int = 250) -> list[dict]:
    """Read every official page, failing closed on drift, duplicates and missing pages."""
    base = source_base(info)
    total = info.get("files")
    if not isinstance(total, int) or isinstance(total, bool) or not 1 <= total <= 10000:
        raise ValueError("Invalid official file total")
    files, seen, basenames = [], set(), set()
    for offset in range(0, total, page_size):
        page = get_page(offset, page_size)
        pagination = page.get("pagination", {})
        items = page.get("items")
        if (pagination.get("total") != total or pagination.get("filtered") != total
                or pagination.get("offset") != offset or not isinstance(items, list)
                or len(items) != min(page_size, total - offset)):
            raise ValueError("Incomplete or changing BioStudies pagination")
        for item in items:
            path = safe_file_path(item.get("path"))
            name = PurePosixPath(path).name
            if path.casefold() in seen or name.casefold() in basenames:
                raise ValueError("Duplicate file path/basename in source listing")
            if (str(item.get("isDirectory", "false")).lower() != "false"
                    or item.get("file_owner", STUDY) != STUDY):
                raise ValueError("Unexpected directory/file owner in source listing")
            size = item.get("Size", item.get("size"))
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError("File listing must give a positive exact byte size")
            seen.add(path.casefold()); basenames.add(name.casefold())
            files.append({"path": path, "size": size, "section": item.get("Section"),
                          "url": base + "/Files/" + quote(path, safe="/"), "name": name})
    counts = Counter(f["section"] for f in files)
    if dict(counts) != info.get("sectionFileCounts"):
        raise ValueError("Section counts do not agree with official study info")
    required = {f"{STUDY}.sdrf.txt", f"{STUDY}.idf.txt"}
    metadata = {f["name"] for f in files if f["section"] == f"mt-{STUDY}"}
    raw = [f for f in files if f["section"] == "raw-data"]
    if metadata != required or len(files) != len(raw) + 2 or not raw:
        raise ValueError("Required two metadata files and raw IDAT section are incomplete")
    if any(not f["name"].endswith(("_Grn.idat", "_Red.idat")) for f in raw):
        raise ValueError("Unexpected raw assay type")
    return files


def audit_sdrf_rows(rows: list[dict], expected_people: int = 256) -> dict:
    if not rows:
        raise ValueError("Empty SDRF")
    columns = {"Source Name", "Characteristics[individual]", "Factor Value[time]",
               "Factor Value[diet]", "Array Data File"}
    if not columns.issubset(rows[0]):
        raise ValueError("Required explicit SDRF mapping columns are missing")
    paired, files, samples = defaultdict(dict), set(), {}
    for row in rows:
        person, sample, arm = (row[k].strip() for k in (
            "Characteristics[individual]", "Source Name", "Factor Value[diet]"))
        time = row["Factor Value[time]"].strip()
        if not person or not sample or arm not in {"HDG", "MED", "greenMED"} or time not in {"0", "18"}:
            raise ValueError("Unrecognized person, arm, sample or exact timepoint")
        raw = safe_file_path(row["Array Data File"])
        name = PurePosixPath(raw).name
        match = re.fullmatch(r"(.+)_(Grn|Red)\.idat", name)
        if not match or name in files:
            raise ValueError("Duplicate or unrecognized IDAT channel")
        if row.get("Label") and row["Label"] != {"Grn": "Cy3", "Red": "Cy5"}[match[2]]:
            raise ValueError("SDRF colour label and filename channel disagree")
        files.add(name)
        key = (person, int(time))
        context = (sample, arm, match[1])
        previous = paired[key].get("context")
        if previous is not None and previous != context:
            raise ValueError("Inconsistent SDRF biological sample context")
        if sample in samples and samples[sample] != key:
            raise ValueError("Source Name maps to more than one person/time")
        samples[sample] = key
        paired[key]["context"] = context
        paired[key].setdefault("channels", set()).add(match[2])
        # Channel rows must carry the same nuisance fields; they are not replicates.
        nuisance = tuple(row.get(k, "") for k in (
            "Characteristics[sex]", "Characteristics[age]", "Comment[Array_Sample_Plate]",
            "Comment[Array_Sentrix_ID]", "Comment[Array_Sentrix_Position]"))
        if paired[key].get("nuisance", nuisance) != nuisance:
            raise ValueError("Channel rows disagree on sample nuisance metadata")
        paired[key]["nuisance"] = nuisance
    people = {p for p, _ in paired}
    if len(people) != expected_people or len(paired) != 2 * expected_people or len(rows) != 4 * expected_people:
        raise ValueError("Public people/sample/channel counts changed; review source release")
    arm_people = Counter()
    for person in people:
        visits = [paired.get((person, t)) for t in (0, 18)]
        if any(v is None or v["channels"] != {"Grn", "Red"} for v in visits):
            raise ValueError("Incomplete paired visits or two-colour channels")
        if visits[0]["context"][1] != visits[1]["context"][1]:
            raise ValueError("Diet arm changes within an individual")
        arm_people[visits[0]["context"][1]] += 1
    return {"accession": STUDY, "independent_people": len(people), "biological_samples": len(paired),
            "sdrf_rows": len(rows), "raw_channel_files": len(files),
            "arm_people": dict(sorted(arm_people.items())), "incomplete_pairs": {},
            "raw_names": sorted(files), "status": "metadata_audited_raw_qc_pending",
            "interpretation": "Red/green channels are not independent samples; no RNA availability claim"}


def load_sdrf(path: Path) -> tuple[list[dict], dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    return rows, audit_sdrf_rows(rows)


def validate_raw_mapping(files: list[dict], audit: dict) -> dict:
    raw = [f for f in files if f["section"] == "raw-data"]
    if {f["name"] for f in raw} != set(audit["raw_names"]):
        raise ValueError("Official raw listing and SDRF disagree; missing/extra IDATs")
    return {"raw_files": len(raw), "raw_bytes": sum(f["size"] for f in raw),
            "metadata_files": 2, "sdrf_channels_complete": True}


def mapping_identity(info: dict, files: list[dict]) -> str:
    payload = {"httpLink": source_base(info), "modified": info.get("modified"), "files": files}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
