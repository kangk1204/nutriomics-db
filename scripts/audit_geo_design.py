from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from nutrigenomics.db import get_session_factory
from nutrigenomics.models import GeoSeries
from nutrigenomics.settings import DATA_DIR, DATABASE_URL

SUBJECT_FIELD = re.compile(
    r"(?:subject|patient|participant|volunteer|donor|person|individual)[\s_:#-]*([A-Za-z0-9.-]+)",
    re.IGNORECASE,
)
SUBJECT_SUFFIX = re.compile(
    r"(?:^|[._-])(?:subject|patient|participant|case|person)?[._-]?(\d+)$", re.IGNORECASE
)
HOUR = re.compile(
    r"(?<![A-Za-z0-9])(0|2|3|4|6|8|12|24)\s*(?:h|hr|hrs|hour|hours)(?![A-Za-z])",
    re.IGNORECASE,
)
DAY = re.compile(r"(?<![A-Za-z0-9])(\d+)\s*(?:d|day|days)(?![A-Za-z])", re.IGNORECASE)
WEEK = re.compile(r"(?<![A-Za-z0-9])(\d+)\s*(?:wk|wks|week|weeks)(?![A-Za-z])", re.IGNORECASE)
YEAR = re.compile(r"(?<![A-Za-z0-9])(\d+)\s*(?:yr|yrs|year|years)(?![A-Za-z])", re.IGNORECASE)
SPELLED_YEAR = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _characteristics(raw: list[str] | None) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for item in raw or []:
        key, separator, value = str(item).partition(":")
        if not separator:
            parsed.setdefault("_unparsed", []).append(str(item).strip())
            continue
        parsed.setdefault(key.strip().casefold(), []).append(value.strip())
    return parsed


def _subject_id(title: str, chars: dict[str, list[str]]) -> tuple[str | None, str]:
    for key, values in chars.items():
        if values and any(
            token in key
            for token in ("subject", "patient", "participant", "donor", "individual", "person")
        ):
            return values[0], f"characteristic:{key}"
    match = SUBJECT_FIELD.search(title)
    if match:
        return match.group(1).rstrip("._-"), "title_label"
    match = SUBJECT_SUFFIX.search(title)
    if match:
        return match.group(1), "title_numeric_suffix_candidate"
    return None, "unresolved"


def _first_matching(chars: dict[str, list[str]], fragments: tuple[str, ...]) -> str | None:
    for key, values in chars.items():
        if any(fragment in key for fragment in fragments) and values:
            return " | ".join(values)
    return None


def _timepoint(text: str) -> str | None:
    normalized = text.casefold().replace("_", " ").replace("-", " ")
    if re.search(
        r"\b(before|baseline|base|beginning|start|pre intervention|pre supplementation|pre diet)\b",
        normalized,
    ):
        return "baseline"
    if re.search(r"\btime\s+0\b", normalized):
        return "baseline"
    if re.search(r"\b(fasting|fast|0h|0 hr|0 hour|0 hours)\b", normalized):
        return "0h"
    match = HOUR.search(normalized)
    if match:
        return f"{match.group(1)}h"
    match = DAY.search(normalized)
    if match:
        return f"{match.group(1)}d"
    match = WEEK.search(normalized)
    if match:
        return f"{match.group(1)}w"
    match = YEAR.search(normalized)
    if match:
        return f"{match.group(1)}y"
    for word, number in SPELLED_YEAR.items():
        if re.search(rf"\b{word}\s+years?\b", normalized):
            return f"{number}y"
    match = re.search(r"\b(after|post)\s+(\d+)\s*(?:days?|weeks?|months?)\b", normalized)
    if match:
        suffix = "w" if "week" in normalized else "m" if "month" in normalized else "d"
        return f"{match.group(2)}{suffix}"
    if re.search(r"\b(after|post intervention|post supplementation|post diet)\b", normalized):
        return "post_unspecified"
    return None


def _study_fields(series: GeoSeries) -> tuple[str | None, str | None, str | None]:
    title = series.title or ""
    platform = ",".join(series.platform_ids or []) or None
    assay = series.assay_type
    if len(series.platform_ids or []) > 1:
        assay_group = "mixed assays"
    elif "methylation" in (assay or "").casefold() or any(
        "GPL13534" in x or "GPL21145" in x for x in (series.platform_ids or [])
    ):
        assay_group = "DNA methylation"
    elif "expression" in (assay or "").casefold():
        assay_group = "gene expression"
    else:
        assay_group = None
    return assay_group, platform, title


def _candidate_timepoint(
    title: str, chars: dict[str, list[str]], metadata: dict[str, Any]
) -> str | None:
    paired_channels = (metadata.get("channel_count") or [None])[0] == "2" and bool(
        metadata.get("source_name_ch2")
    )
    channel_sources = [] if paired_channels else metadata.get("source_name_ch1") or []
    for value in (title, *(str(item) for item in channel_sources)):
        candidate = _timepoint(value)
        if candidate:
            return candidate
    for key, values in chars.items():
        normalized_key = re.sub(r"[^a-z0-9]+", " ", key).strip()
        if (
            normalized_key in {"time", "time point", "timepoint", "visit", "visit number"}
            and values
        ):
            candidate = _timepoint(values[0])
            if candidate:
                return candidate
    return None


def _channel_timepoints(metadata: dict[str, Any]) -> dict[str, str]:
    channel_timepoints: dict[str, str] = {}
    for channel in ("ch1", "ch2"):
        source_names = metadata.get(f"source_name_{channel}") or []
        for value in source_names:
            candidate = _timepoint(str(value))
            if candidate:
                channel_timepoints[channel] = candidate
                break
    return channel_timepoints


def audit_design(series: GeoSeries) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    subject_counts: Counter[str] = Counter()
    time_counts: Counter[str] = Counter()
    channel_time_counts: Counter[str] = Counter()
    missing_subjects = 0
    for sample in sorted(series.samples, key=lambda item: item.sample_accession):
        chars = _characteristics(sample.characteristics)
        title = sample.title or ""
        metadata = sample.metadata_json or {}
        subject_id, subject_source = _subject_id(title, chars)
        subject_field = _first_matching(
            chars, ("subject", "patient", "participant", "donor", "individual", "person")
        )
        condition = _first_matching(
            chars,
            ("treatment", "diet", "meal", "group", "intervention", "supplement", "status", "dose"),
        )
        if (
            subject_id is not None
            and subject_source == "title_numeric_suffix_candidate"
            and condition
        ):
            subject_id = f"{condition.casefold()}#{subject_id}"
        timepoint = _candidate_timepoint(title, chars, metadata)
        channel_timepoints = _channel_timepoints(metadata)
        channel_labels = {
            channel: (metadata.get(f"label_{channel}") or [None])[0]
            for channel in ("ch1", "ch2")
            if metadata.get(f"label_{channel}")
        }
        platform = metadata.get("platform_id")
        if isinstance(platform, list):
            platform = " | ".join(str(item) for item in platform)
        if subject_id is None:
            missing_subjects += 1
        else:
            subject_counts[subject_id] += 1
        if timepoint:
            time_counts[timepoint] += 1
        for channel_timepoint in channel_timepoints.values():
            channel_time_counts[channel_timepoint] += 1
        rows.append(
            {
                "sample_accession": sample.sample_accession,
                "title": title,
                "subject_candidate": subject_id,
                "subject_field": subject_field,
                "subject_parse_source": subject_source,
                "timepoint_candidate": timepoint,
                "condition_candidate": condition,
                "platform_id": platform,
                "channel_characteristics_json": json.dumps(
                    metadata.get("channel_characteristics", {}), ensure_ascii=False
                ),
                "channel_source_names_json": json.dumps(
                    {
                        key: value
                        for key, value in metadata.items()
                        if key.casefold().startswith("source_name_ch")
                    },
                    ensure_ascii=False,
                ),
                "channel_labels_json": json.dumps(
                    {
                        key: value
                        for key, value in metadata.items()
                        if key.casefold().startswith("label_ch")
                    },
                    ensure_ascii=False,
                ),
                "channel_timepoints_json": json.dumps(channel_timepoints, ensure_ascii=False),
                "paired_channel_design_json": json.dumps(
                    {
                        channel: {
                            "timepoint_candidate": channel_timepoints.get(channel),
                            "label": channel_labels.get(channel),
                            "source_name": metadata.get(f"source_name_{channel}", []),
                        }
                        for channel in ("ch1", "ch2")
                        if f"source_name_{channel}" in metadata or channel in channel_labels
                    },
                    ensure_ascii=False,
                ),
                "characteristics_json": json.dumps(
                    sample.characteristics or [], ensure_ascii=False
                ),
                "sample_metadata_json": json.dumps(metadata, ensure_ascii=False),
            }
        )
    duplicate_groups = {subject: count for subject, count in subject_counts.items() if count > 1}
    assay_group, platform, title = _study_fields(series)
    summary = {
        "accession": series.accession,
        "study_id": series.study_id,
        "title": title,
        "assay_group": assay_group,
        "series_platform_ids": series.platform_ids or [],
        "sample_count_db": len(rows),
        "sample_count_series": series.sample_count,
        "subjects_parsed": len(subject_counts),
        "samples_without_subject_candidate": missing_subjects,
        "repeated_subject_candidate_count": len(duplicate_groups),
        "repeated_subject_counts": duplicate_groups,
        "timepoint_counts": dict(time_counts),
        "channel_timepoint_counts": dict(channel_time_counts),
        "design_interpretation": (
            "metadata audit only; candidate fields are not analysis-ready until checked against "
            "the publication and raw sample mapping"
        ),
        "needs_manual_review": bool(
            missing_subjects
            or not time_counts
            or not duplicate_groups
            or (series.sample_count is not None and series.sample_count != len(rows))
        ),
    }
    return rows, summary


def audit_database(
    database_url: str = DATABASE_URL, output_dir: Path | None = None
) -> dict[str, Any]:
    session = get_session_factory(database_url)()
    output_dir = (
        (output_dir or (DATA_DIR.parent / "outputs" / "geo-design-audit")).expanduser().resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    try:
        series_rows = list(session.scalars(select(GeoSeries).order_by(GeoSeries.accession)))
        for series in series_rows:
            series = session.get(GeoSeries, series.accession)
            rows, summary = audit_design(series)
            target = output_dir / series.accession
            target.mkdir(parents=True, exist_ok=True)
            table_path = target / "sample_design.tsv"
            columns = [
                "sample_accession",
                "title",
                "subject_candidate",
                "subject_field",
                "subject_parse_source",
                "timepoint_candidate",
                "condition_candidate",
                "platform_id",
                "characteristics_json",
                "sample_metadata_json",
                "channel_characteristics_json",
                "channel_source_names_json",
                "channel_labels_json",
                "channel_timepoints_json",
                "paired_channel_design_json",
            ]
            with table_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle, fieldnames=columns, delimiter="\t", lineterminator="\n"
                )
                writer.writeheader()
                writer.writerows(rows)
            summary["sample_design_path"] = str(table_path)
            summary["audited_utc"] = datetime.now(UTC).isoformat()
            (target / "audit.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            summaries.append(summary)
    finally:
        session.close()
    return {"output_dir": str(output_dir), "series_count": len(summaries), "studies": summaries}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit GEO sample metadata and candidate repeated-measures structure"
    )
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(audit_database(args.database_url, args.output_dir), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
