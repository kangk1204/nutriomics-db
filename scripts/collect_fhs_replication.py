"""Original ST3/ST4 cohort-specific eQTM replication, not dietary validation."""

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nutrigenomics.expansion import connect, migrate
from nutrigenomics.explorer_build import canonical, sha256
from nutrigenomics.source_updates import atomic_json


def collect(workbook, output):
    workbook, output = Path(workbook), Path(output)
    if output.exists():
        raise FileExistsError("immutable replication sidecar")
    digest = sha256(workbook)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_suffix(".building")
    db = connect(staging)
    migrate(db)
    sid = "FHS-replication:" + digest[:16]
    db.execute(
        "INSERT INTO ng_release VALUES(?,?,?,?,?,?,?,?,?)",
        (
            sid,
            "FHS-eQTM-replication",
            "2023-ST3-ST4",
            "https://www.nature.com/articles/s41598-023-39936-3",
            "2026-10-09",
            digest,
            str(workbook),
            "Published supplement; redistribution review pending",
            "public",
        ),
    )
    for cohort, parent_n in [("WHI", 1248), ("JHS", 521)]:
        db.execute(
            "INSERT INTO ng_study VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "FHS-replication:" + cohort,
                cohort + " published whole-blood eQTM replication",
                "Original publication replication cohort; pair-specific N not reported in this table",
                "observational",
                cohort,
                "verified",
                "Distinct replication cohort as reported in original study",
                parent_n,
                "37563237",
                None,
                canonical(
                    {
                        "n_people_scope": "publication parent cohort, not per-pair N",
                        "not_dietary_external_validation": True,
                    }
                ),
            ),
        )
    count = 0
    missing = 0
    try:
        for sheet, kind in [("ST3", "cis"), ("ST4", "trans")]:
            frame = pd.read_excel(workbook, sheet_name=sheet, header=None)
            if list(frame.iloc[2, :2]) != ["eGene", "CpG"] or frame.shape[1] != 14:
                raise ValueError("replication table schema changed")
            for index, row in frame.iloc[3:].iterrows():
                gene, probe = str(row.iloc[0]), str(row.iloc[1])
                if not re.fullmatch(r"ENSG\d+(?:\.\d+)?", gene) or not re.fullmatch(
                    r"(cg\d{8}|ch\.[0-9XY]+\.\d+[FR])", probe
                ):
                    raise ValueError("original replication identifier not recognized")
                for identifier, ns, kind_entity in [
                    (gene, "Ensembl", "gene"),
                    (probe, "Illumina", "molecular_feature"),
                ]:
                    db.execute(
                        "INSERT OR IGNORE INTO ng_entity VALUES(?,?,?,?,?,?,?)",
                        (
                            ns + ":" + identifier,
                            kind_entity,
                            identifier,
                            ns,
                            identifier,
                            "Homo sapiens",
                            "{}",
                        ),
                    )
                for cohort, start in [("WHI", 6), ("JHS", 10)]:
                    numbers = [
                        None if pd.isna(v) else float(v) for v in row.iloc[start : start + 4]
                    ]
                    if any(v is not None and not math.isfinite(v) for v in numbers):
                        raise ValueError("nonfinite replication statistic")
                    effect, se, t, p = numbers
                    if p is not None and not 0 <= p <= 1:
                        raise ValueError("invalid original p")
                    if effect is None:
                        missing += 1
                    context = {
                        "review_status": "source_checked",
                        "export_allowed": False,
                        "replication_cohort": cohort,
                        "genome_build": "unverified",
                        "sheet": sheet,
                        "excel_row": int(index + 1),
                        "source_table_selection": "top 1000 eGenes, not all tested pairs",
                        "original_p_value": "source-reported zero; precision/underflow unresolved"
                        if p == 0
                        else p,
                        "discovery_estimate": None if pd.isna(row.iloc[2]) else float(row.iloc[2]),
                        "original_t_statistic": t,
                        "n_people_scope": "pair-specific N not reported",
                        "limitations": [
                            "External eQTM replication is not an external dietary response validation.",
                            "Cohort-specific expression platforms and coefficient scales are not pooled.",
                            "Missing QC-excluded gene result is not a zero effect.",
                        ],
                    }
                    db.execute(
                        """INSERT INTO ng_evidence(id,subject_id,object_id,relation,layer,study_id,
                        release_id,source_ref,tier,inference,organism,tissue,effect,effect_unit,standard_error,
                        p_value,value_status,context_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            f"FHS-replication:{sheet}:{cohort}:{gene}:{probe}:{digest[:16]}",
                            "Illumina:" + probe,
                            "Ensembl:" + gene,
                            kind + "_eqtm_replication",
                            "molecular_target",
                            "FHS-replication:" + cohort,
                            sid,
                            f"{sheet} Excel row {index + 1}, {cohort} columns",
                            "human_observational",
                            "association",
                            "Homo sapiens",
                            "whole blood",
                            effect,
                            "source_cohort_expression_regression_coefficient"
                            if effect is not None
                            else None,
                            se,
                            p if p != 0 else None,
                            "measured" if effect is not None else "not_reported",
                            canonical(context),
                        ),
                    )
                    count += 1
        db.commit()
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("replication foreign-key failure")
        db.close()
        os.replace(staging, output)
        report = {
            "source_sha256": digest,
            "sidecar_sha256": sha256(output),
            "records": count,
            "missing_results": missing,
            "expert_review_status": "pending",
            "export_allowed": False,
            "dietary_external_validation": False,
        }
        atomic_json(output.with_suffix(".receipt.json"), report)
        return report
    except BaseException:
        db.close()
        raise


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workbook", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(collect(a.workbook, a.output)))
