"""Anonymous LC/MS feature measurements; no structural or health inference."""

import csv
import io
import math
import re
from collections import Counter, defaultdict

VERSION = "nutrition-metabolomics-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS ng_molecular_sample(
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES ng_dataset(id),
 source_label TEXT NOT NULL, source_sample_id TEXT NOT NULL,
 sample_type TEXT NOT NULL CHECK(sample_type IN ('food','urine')),
 participant_key TEXT, arm_id TEXT REFERENCES ng_arm(id), time_label TEXT,
 food_entity_id TEXT REFERENCES ng_entity(id),
 unit TEXT NOT NULL, missing_count INTEGER NOT NULL CHECK(missing_count>=0),
 release_id TEXT NOT NULL REFERENCES ng_release(id), context_json TEXT NOT NULL,
 CHECK((sample_type='food' AND participant_key IS NULL AND food_entity_id IS NOT NULL AND arm_id IS NULL)
 OR (sample_type='urine' AND participant_key IS NOT NULL AND food_entity_id IS NULL AND arm_id IS NOT NULL)),
 UNIQUE(dataset_id,source_sample_id));
CREATE TABLE IF NOT EXISTS ng_molecular_measurement(
 sample_id TEXT NOT NULL REFERENCES ng_molecular_sample(id),
 feature_id TEXT NOT NULL REFERENCES ng_entity(id),
 value REAL NOT NULL, value_status TEXT NOT NULL CHECK(value_status IN ('measured','measured_zero')),
 release_id TEXT NOT NULL REFERENCES ng_release(id), PRIMARY KEY(sample_id,feature_id),
 CHECK(value_status!='measured_zero' OR value=0));
CREATE INDEX IF NOT EXISTS ng_molecular_measurement_feature ON ng_molecular_measurement(feature_id);
CREATE VIEW IF NOT EXISTS ng_food_urine_feature_path AS
 SELECT f.dataset_id,f.food_entity_id,f.id AS food_sample,u.id AS urine_sample,u.participant_key,
 fm.feature_id,fm.value AS food_value,um.value AS urine_value,f.unit,
 0 AS quantity_transfer_allowed,0 AS compound_identity_established,0 AS health_attribution_allowed
 FROM ng_molecular_sample f JOIN ng_molecular_measurement fm ON fm.sample_id=f.id
 JOIN ng_molecular_measurement um ON um.feature_id=fm.feature_id
 JOIN ng_molecular_sample u ON u.id=um.sample_id AND u.dataset_id=f.dataset_id
 WHERE f.sample_type='food' AND u.sample_type='urine';
"""


def migrate_metabolomics(db):
    db.executescript(SCHEMA)
    with db:
        db.execute("INSERT OR IGNORE INTO ng_migration VALUES(?,datetime('now'))", (VERSION,))


def factors(text):
    result = {}
    for part in text.split("|"):
        key, value = part.strip().split(":", 1)
        if key in result:
            raise ValueError("Repeated source factor")
        result[key] = value.strip()
    return result


def parse_source(raw, metadata, analysis, mwtab):
    if analysis["study_id"] != "ST001257" or analysis["analysis_id"] != "AN002086":
        raise ValueError("Analysis identity mismatch")
    if analysis["units"] != "Abundance (Counts Log2 Transformed)":
        raise ValueError("Unreviewed units; do not transform")
    if "Has m/z:Neutral masses" not in mwtab or "RT units:Minutes" not in mwtab:
        raise ValueError("Feature-label semantics not verified")
    reader = csv.reader(io.StringIO(raw.decode("utf-8")), delimiter="\t")
    header = next(reader)
    if header[:2] != ["Samples", "group"] or len(set(header[2:])) != len(header) - 2:
        raise ValueError("Matrix header or repeated feature")
    features = header[2:]
    if any(not re.fullmatch(r"\d+(?:\.\d+)?_\d+(?:\.\d+)?", f) for f in features):
        raise ValueError("Unreviewed feature label")
    records = list(metadata.values())
    if any(r["study_id"] != "ST001257" for r in records):
        raise ValueError("Factor study mismatch")
    lookup = {r["local_sample_id"]: r for r in records}
    if len(lookup) != len(records) or len({r["mb_sample_id"] for r in records}) != len(records):
        raise ValueError("Duplicate source sample")
    samples = []
    seen = set()
    people = defaultdict(set)
    counts = Counter()
    conflicts = []
    for row in reader:
        if len(row) != len(header) or row[0] in seen or row[0] not in lookup:
            raise ValueError("Sample alignment/uniqueness mismatch")
        seen.add(row[0])
        meta = lookup[row[0]]
        group = factors(meta["factors"])
        if factors(row[1]) != group:
            raise ValueError("Matrix and authoritative factors disagree")
        sample_type = group["Sample type"].lower()
        if sample_type not in {"food", "urine"}:
            raise ValueError("Unreviewed sample type")
        context = dict(meta)
        context["reviewed_sample_type_basis"] = "Explicit factor; official study sample table"
        participant = None
        time = None
        arm = None
        if meta["sample_source"].lower() != sample_type:
            conflicts.append(
                {
                    "sample": row[0],
                    "field": "sample_source",
                    "reported": meta["sample_source"],
                    "factor": sample_type,
                }
            )
        if sample_type == "urine":
            match = re.match(r"^S31-(\d+)\b", row[0])
            condition = group["Pre or Post and food"]
            if not match or condition not in {
                "Pre Chicken",
                "Post Chicken",
                "Pre Pork",
                "Post Pork",
            }:
                raise ValueError("Unresolved participant/condition")
            participant = "ST001257:S31-" + match.group(1)
            time, arm = condition.split(" ")
            if condition in people[participant]:
                raise ValueError("Repeated participant-condition")
            people[participant].add(condition)
            suffix = re.search(r"(Ch|Po)$", row[0])
            if suffix and {"Ch": "Chicken", "Po": "Pork"}[suffix.group(1)] != arm:
                conflicts.append(
                    {
                        "sample": row[0],
                        "field": "title_arm",
                        "reported": suffix.group(1),
                        "factor": arm,
                    }
                )
        values = []
        missing = 0
        for token in row[2:]:
            if token == "NA":
                values.append(None)
                missing += 1
                counts["missing"] += 1
            else:
                value = float(token)
                if not math.isfinite(value):
                    raise ValueError("Nonfinite token is not a reviewed missing value")
                values.append(value)
                counts["finite"] += 1
                counts["zero"] += value == 0
        counts[sample_type] += 1
        context["period_sequence_unknown"] = True
        context["raw_data_file_downloaded"] = False
        samples.append(
            {
                "id": "NMDR:AN002086:" + meta["mb_sample_id"],
                "source_id": meta["mb_sample_id"],
                "label": row[0],
                "sample_type": sample_type,
                "participant": participant,
                "time": time,
                "arm": arm,
                "food_type": group["Food type"],
                "values": values,
                "missing": missing,
                "context": context,
            }
        )
    if seen != set(lookup):
        raise ValueError("Source samples absent from matrix")
    if any(p != {"Pre Chicken", "Post Chicken", "Pre Pork", "Post Pork"} for p in people.values()):
        raise ValueError("Incomplete four-condition participants")
    return (
        features,
        samples,
        dict(
            counts,
            n_people=len(people),
            n_features=len(features),
            n_cells=len(samples) * len(features),
            conflicts=conflicts,
            unit=analysis["units"],
            mass_label="Neutral masses",
            retention_time_unit="Minutes",
            raw_or_processed="Deposited processed log2 abundance; no second log transform",
            missing_policy="Only explicit NA tokens; missing cells excluded from observed-measurement table, counted per sample",
        ),
    )
