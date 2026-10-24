"""Versioned batch jobs, shared CPU slots, freeze receipts and real soak records."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import time

from .db import connect, digest, dumps, now


def model_home(settings):
    return Path(os.environ.get("LITERATURE_V6_MODEL_HOME", str(settings.home / "v6" / "models")))


@contextmanager
def processing_slot(settings):
    """Cross-process flock slots shared by OA, legacy and v4 batch processing."""
    if os.name != "posix":
        # Desktop validation only; operational deployment is Linux.
        yield
        return
    import fcntl

    directory = Path(os.environ.get("LITERATURE_PROCESSING_LOCK_HOME", str(settings.home / "locks")))
    directory.mkdir(parents=True, exist_ok=True)
    handles = [(directory / f"processing-{i}.lock").open("a+") for i in range(2)]
    acquired = None
    try:
        while acquired is None:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = handle
                    break
                except BlockingIOError:
                    continue
            if acquired is None:
                time.sleep(0.2)
        yield
    finally:
        if acquired is not None:
            fcntl.flock(acquired, fcntl.LOCK_UN)
        for handle in handles:
            handle.close()


def components(settings, provider="union"):
    from .candidates_v4 import GeneIndex
    from .provider_v6 import SharedCandidates
    from .clinical_v4 import ClinicalModels
    from .models_v5 import LearnedModels
    from .models_v6 import Models
    from .identity_v6 import CandidateSearch
    from .embeddings_v6 import FrozenEncoder
    from .chemical_v6 import load_snapshot

    path = model_home(settings)
    pointer = json.loads((path / "gene-snapshot.json").read_bytes())
    gene = GeneIndex.load(path / "genes" / pointer["filename"])
    encoder = FrozenEncoder(path / "gene" / "selected_model", settings.home / "embedding-cache-v6")
    encoder.load()
    candidates = SharedCandidates(path, encoder, provider)
    candidates.load()
    clinical = ClinicalModels(Path(os.environ.get("LITERATURE_V6_CLINICAL_HOME", str(path / "clinical"))))
    clinical.load()
    learned = LearnedModels(
        Path(os.environ.get("LITERATURE_V6_LEARNED_HOME", str(path / "learned-v5"))),
        os.environ.get("LITERATURE_V6_RELATION_CONFIGURATION"),
        float(os.environ["LITERATURE_V6_RELATION_THRESHOLD"])
        if "LITERATURE_V6_RELATION_THRESHOLD" in os.environ
        else None,
        span_provider=provider,
    )
    if os.environ.get("LITERATURE_V6_SCOPE_HOME"):
        from .scope_recovery_v6 import RecoveredScope

        learned = RecoveredScope(os.environ["LITERATURE_V6_SCOPE_HOME"])
    learned.load()
    models = Models(path / "learned-v6")
    models.load()
    for record in models.manifest.get("clinical_heads", {}).values():
        if record.get("upstream_clinical_audit", {}).get("manifest_sha256") != clinical.manifest.get(
            "manifest_sha256"
        ):
            raise ValueError("Clinical proposal model differs from audited head training")
    chemical_pointer = json.loads((path / "mesh-snapshot.json").read_bytes())
    chemical = load_snapshot(path / "mesh" / chemical_pointer["filename"], chemical_pointer["sha256"])
    return {
        "_gene_index": gene,
        "_candidate_provider": candidates,
        "_clinical_models": clinical,
        "_learned_models": learned,
        "_v6_models": models,
        "_encoder": encoder,
        "_id_search": CandidateSearch(gene),
        "_chemical_lexicon": chemical,
    }


def identity(settings, values, dictionaries, profile="local", external_sha=None):
    from importlib.metadata import version
    from .extraction_v6 import source_manifest
    from .nlp_v2 import parser_for, parser_manifest
    from .resources_v6 import POLICY

    hashes, rules = source_manifest()
    parser = parser_for({})
    if not parser_manifest(parser).get("model_sha256"):
        # The parser is lazy: initialize before writing a stable batch identity.
        parser.parse({"text": "Model initialization.", "start": 0, "end": 21})
    parser_record = parser_manifest(parser)
    if not parser_record.get("available") or not parser_record.get("model_sha256"):
        raise ValueError("Production parser identity is unavailable")
    result = {
        "engine_version": "heuristic-v6",
        "rules_version": rules,
        "rule_source_hashes": hashes,
        "candidate_provider": values["_candidate_provider"].manifest,
        "clinical_models": values["_clinical_models"].manifest,
        "learned_models": values["_learned_models"].manifest,
        "v6_models": values["_v6_models"].manifest,
        "frozen_encoder": values["_encoder"].manifest,
        "chemical_dictionary": values["_chemical_lexicon"]["manifest"],
        "gene_dictionary": values["_gene_index"].manifest,
        "dictionary_version": dictionaries.version
        + "|ncbi:"
        + values["_gene_index"].manifest["records_sha256"]
        + "|mesh:"
        + values["_chemical_lexicon"]["manifest"]["snapshot_sha256"],
        "model_sha256": parser_record["model_sha256"],
        "profile": profile,
        "resource_policy": POLICY,
        "external_response_sha256": external_sha,
        "dependencies": {
            name: version(name)
            for name in (
                "spacy",
                "scispacy",
                "torch",
                "transformers",
                "numpy",
                "scikit-learn",
                "sklearn-crfsuite",
            )
        },
        "evaluator_sha256": digest((Path(__file__).parent / "evaluation_v6.py").read_bytes()),
    }
    result["artifact_identity"] = digest(dumps(result))
    return result


def register(settings, values, dictionaries, queue_all=False):
    from .pipeline import enqueue_engine

    record = identity(settings, values, dictionaries)
    directory = settings.home / "engine-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "heuristic-v6.tmp"
    temporary.write_text(dumps(record), encoding="utf-8")
    temporary.replace(directory / "heuristic-v6.json")
    profile = {
        "engine_version": "heuristic-v6",
        "rules_version": record["rules_version"],
        "dictionary_version": record["dictionary_version"],
        "artifact_version": record["artifact_identity"],
    }
    count = 0
    with connect(settings) as con:
        con.execute(
            "INSERT INTO engine_profiles(engine_version,rules_version,dictionary_version,enabled,updated_at,artifact_version) "
            "VALUES(?,?,?,1,?,?) ON CONFLICT(engine_version) DO UPDATE SET rules_version=excluded.rules_version,"
            "dictionary_version=excluded.dictionary_version,artifact_version=excluded.artifact_version,enabled=1,updated_at=excluded.updated_at",
            (
                profile["engine_version"],
                profile["rules_version"],
                profile["dictionary_version"],
                now(),
                profile["artifact_version"],
            ),
        )
        con.execute(
            "UPDATE engine_queue SET status='superseded' WHERE engine_version='heuristic-v6' "
            "AND status IN ('pending','failed') AND artifact_version<>?",
            (profile["artifact_version"],),
        )
        rows = con.execute(
            "SELECT p.paper_id,s.text_hash FROM papers p JOIN sources s ON s.source_id=p.current_source_id"
        ).fetchall()
        for row in rows:
            enqueue_engine(con, row["paper_id"], row["text_hash"], profile, retry=queue_all)
            count += 1
    return {"profile": profile, "queued": count, "artifact": record}


def process(settings, limit=10, values=None, dictionaries=None):
    import psutil
    from .dictionaries import load_dictionaries
    from .registry_v6 import extract_engine
    from .pipeline import store_extraction
    from .db import start_run, finish_run

    dictionaries = dictionaries or load_dictionaries(settings, "heuristic-v2")
    values = values or components(settings)
    registration = register(settings, values, dictionaries)
    artifact = registration["artifact"]
    counts = {"engine": "heuristic-v6", "processed": 0, "failed": 0, "superseded": 0}
    run = start_run(settings, "process_extract_heuristic-v6")
    try:
        with processing_slot(settings):
            for _ in range(limit):
                if (
                    psutil.Process().memory_info().rss > 8 * 1024**3
                    or psutil.virtual_memory().available < 2 * 1024**3
                ):
                    counts["throttled"] = True
                    break
                with connect(settings) as con:
                    con.execute("BEGIN IMMEDIATE")
                    task = con.execute(
                        "SELECT * FROM engine_queue WHERE engine_version='heuristic-v6' AND status='pending' "
                        "AND due_at<=? ORDER BY task_id LIMIT 1",
                        (now(),),
                    ).fetchone()
                    if not task:
                        break
                    task = dict(task)
                    paper = dict(
                        con.execute("SELECT * FROM papers WHERE paper_id=?", (task["paper_id"],)).fetchone()
                    )
                    source = con.execute(
                        "SELECT * FROM sources WHERE source_id=?", (paper["current_source_id"],)
                    ).fetchone()
                    if (
                        not source
                        or source["text_hash"] != task["source_hash"]
                        or task["artifact_version"] != artifact["artifact_identity"]
                    ):
                        con.execute(
                            "UPDATE engine_queue SET status='superseded' WHERE task_id=?", (task["task_id"],)
                        )
                        counts["superseded"] += 1
                        continue
                    source = dict(source)
                    con.execute(
                        "UPDATE engine_queue SET status='running',attempts=attempts+1,due_at=? WHERE task_id=?",
                        (now(), task["task_id"]),
                    )
                try:
                    metadata = {
                        **paper,
                        **values,
                        "publication_types": json.loads(paper["publication_types"]),
                        "mesh": json.loads(paper["mesh"]),
                        "_artifact_identity": artifact["artifact_identity"],
                    }
                    result = extract_engine(
                        source["text_content"],
                        json.loads(source["sections"]),
                        dictionaries,
                        metadata,
                        "heuristic-v6",
                    )
                    store_extraction(settings, paper["paper_id"], source, result)
                    with connect(settings) as con:
                        con.execute(
                            "UPDATE engine_queue SET status='completed',error=NULL WHERE task_id=?",
                            (task["task_id"],),
                        )
                    counts["processed"] += 1
                except Exception as error:
                    counts["failed"] += 1
                    with connect(settings) as con:
                        con.execute(
                            "UPDATE engine_queue SET status='failed',error=? WHERE task_id=?",
                            (str(error), task["task_id"]),
                        )
        finish_run(
            settings, run, "partial" if counts["failed"] or counts.get("throttled") else "completed", counts
        )
    except BaseException as error:
        finish_run(settings, run, "failed", counts, str(error))
        raise
    return {"run_id": run, **counts}


def observe(settings):
    """Append actual observations; zero-document scheduler ticks are not processing."""
    from .registry_v6 import default_engine

    artifact = settings.home / "engine-artifacts" / "heuristic-v6.json"
    record = json.loads(artifact.read_bytes()) if artifact.exists() else {}
    current_identity = record.get("artifact_identity")
    with connect(settings) as con:
        queue = {
            r["status"]: r["n"]
            for r in con.execute(
                "SELECT status,count(*) n FROM engine_queue WHERE engine_version='heuristic-v6' AND artifact_version=? GROUP BY status",
                (current_identity,),
            )
        }
        history = con.execute(
            "SELECT count(DISTINCT paper_id) FROM extractions WHERE engine_version='heuristic-v6'"
        ).fetchone()[0]
        processed = con.execute(
            "SELECT count(DISTINCT paper_id) FROM extractions WHERE engine_version='heuristic-v6' AND json_extract(result_json,'$.manifest.artifact_identity')=?",
            (current_identity,),
        ).fetchone()[0]
        active = con.execute(
            "SELECT count(*) FROM extractions e JOIN papers p USING(paper_id) WHERE e.engine_version='heuristic-v6' AND e.active=1 AND e.source_id=p.current_source_id AND json_extract(e.result_json,'$.manifest.artifact_identity')=?",
            (current_identity,),
        ).fetchone()[0]
    value = {
        "observed_at": now(),
        "queue": queue,
        "processed_unique_papers": processed,
        "active_papers": active,
        "historical_processed_unique_papers": history,
        "artifact_identity": current_identity,
        "default_engine": default_engine(settings),
        "artifact_sha256": digest(artifact.read_bytes()) if artifact.exists() else None,
    }
    directory = settings.home / "experiments" / "v6"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "operation.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(dumps(value) + "\n")
    return value


def operation_status(settings):
    path = settings.home / "experiments" / "v6" / "operation.jsonl"
    history = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    artifact = settings.home / "engine-artifacts" / "heuristic-v6.json"
    current = digest(artifact.read_bytes()) if artifact.exists() else None
    rows = [row for row in history if current and row.get("artifact_sha256") == current]
    rows.sort(key=lambda row: row["observed_at"])
    dates = [datetime.fromisoformat(r["observed_at"]) for r in rows]
    elapsed = (max(dates) - min(dates)).total_seconds() / 3600 if dates else 0
    moment = datetime.fromisoformat(now())
    cutoff = moment - timedelta(days=7)
    before = [i for i, d in enumerate(dates) if d <= cutoff]
    window = rows[before[-1] :] if before else rows
    window_dates = [datetime.fromisoformat(row["observed_at"]) for row in window]
    coverage = (window_dates[-1] - window_dates[0]).total_seconds() / 3600 if window_dates else 0
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(window_dates, window_dates[1:])]
    maximum_gap = max(gaps, default=0)
    current_age = (moment - dates[-1]).total_seconds() / 60 if dates else None
    seven_days = (
        coverage >= 168
        and len({d.date() for d in window_dates}) >= 8
        and maximum_gap <= 30
        and current_age is not None
        and 0 <= current_age <= 30
        and any(r["processed_unique_papers"] for r in window)
        and not any(r.get("queue", {}).get("failed", 0) for r in window)
    )
    return {
        "engine": "heuristic-v6",
        "status": "observing" if rows else "not_started",
        "samples": len(rows),
        "historical_samples": len(history),
        "artifact_sha256": current,
        "max_gap_minutes": round(maximum_gap, 2),
        "continuous_window_hours": round(coverage, 2),
        "latest_age_minutes": round(current_age, 2) if current_age is not None else None,
        "elapsed_hours": round(elapsed, 2),
        "last": rows[-1] if rows else None,
        "seven_days_observed": seven_days,
        "promotion_eligible": False,
    }


def promotion_valid(settings, record):
    """Fail closed; a manually edited passed flag cannot produce promotion."""
    try:
        from .registry_v6 import engine_rules_version

        receipt = record["promotion"]
        path = settings.home / "experiments" / "v6" / "promotion.json"
        if (
            not path.exists()
            or json.loads(path.read_bytes()) != receipt
            or digest(dumps(receipt)) != record["promotion_sha256"]
        ):
            return False
        gates = receipt["gates"]
        required = {
            "candidate_recall",
            "id_retrieval",
            "strict_id_gain",
            "clinical_outcome_gain",
            "gene",
            "relation_oracle",
            "relation_automatic",
            "scope_abstract",
            "scope_fulltext",
            "provenance",
            "regression",
            "backup_restore",
            "resume",
            "switch_rollback",
            "seven_days",
        }
        if set(gates) != required or not all(v is True for v in gates.values()):
            return False
        if record["rules_version"] != engine_rules_version("heuristic-v6"):
            return False
        from . import benchmarks as scoring

        final = receipt.get("independent_final_report", {})
        if (
            not scoring.verify_receipt(settings, "v6_final_evaluation", final)
            or final.get("schema") != "v6-independent-bc8-final"
        ):
            return False
        artifact = json.loads((settings.home / "engine-artifacts/heuristic-v6.json").read_bytes())
        if final.get("artifact") != artifact or final.get("papers") != 400:
            return False
        normalized = receipt.get("independent_corpus_reports", [])
        if len(normalized) != 4 or {r.get("corpus") for r in normalized} != {
            "nlmgene",
            "nlmchem",
            "pubmedrct",
            "evidenceinference",
        }:
            return False
        if any(
            not scoring.verify_receipt(settings, "v6_final_evaluation", report)
            or report.get("schema") != "v6-independent-normalized-final"
            or report.get("artifact") != artifact
            or report.get("source_protection_verified") is not True
            for report in normalized
        ):
            return False
        drill = receipt.get("operation_drill", {})
        if (
            not scoring.verify_receipt(settings, "v6_operation_drill", drill)
            or drill.get("artifact_identity") != artifact["artifact_identity"]
        ):
            return False
        if not drill.get("online_backup_restore", {}).get("passed") or not drill.get(
            "interrupted_task_resume", {}
        ).get("passed"):
            return False
        for name, precision, recall in (
            ("gene", 0.9, 0.7),
            ("relation", 0.85, 0.3),
            ("relation_direction_all", 0.85, 0.3),
            ("verified_relation", 0.85, 0.3),
        ):
            metric = final.get("metrics", {}).get(name, {})
            if (metric.get("precision") or 0) < precision or (metric.get("recall") or 0) < recall:
                return False
        for name in ("relation", "relation_direction_all", "verified_relation"):
            metric = final.get("oracle_metrics", {}).get(name, {})
            if (metric.get("precision") or 0) < 0.85 or (metric.get("recall") or 0) < 0.3:
                return False
        return operation_status(settings)["seven_days_observed"]
    except (OSError, ValueError, TypeError, KeyError):
        return False
