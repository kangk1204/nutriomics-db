"""Prepare a preregistered EBM expert-test supplement without changing frozen code.

This helper never predicts. It inventories archive headers, audits the protocol,
then copies selected expert labels into paths supported by the frozen adapter.
"""

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nutri_literature import benchmarks  # noqa: E402
from nutri_literature.db import digest, dumps, now  # noqa: E402
from nutri_literature.public_corpora import prepare_public  # noqa: E402
from nutri_literature.settings import Settings  # noqa: E402

VERSION = "ebm-expert-supplement-v1"
OFFICIAL_ARCHIVE_SHA256 = "b7357503911ba9f708d04e24c1ab3fe9e0a79833910e53e2472ed21214a44e3f"
OFFICIAL_URL = "https://raw.githubusercontent.com/bepnye/EBM-NLP/master/ebm_nlp_2_00.tar.gz"
EXPECTED_GOLD_UNION = 191
EXPECTED_SELECTED = 187
EXCLUSIONS = {
    "22865017": "Previously exposed in EvidenceOutcomes development",
    "24350813": "Previously exposed in EvidenceOutcomes development",
    "25369243": "Previously exposed in EvidenceOutcomes development",
    "23549581": "Already predicted in EvidenceOutcomes protected test; immutable source differs",
}
TASKS = ("participants", "interventions", "outcomes")
PROTOCOL_KIND = "ebm_expert_supplement_protocol"
DERIVATION_KIND = "ebm_expert_supplement_derivation"


def _file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _helper_sha():
    return _file_sha(__file__)


def _inventory(archive_path):
    """Read member headers only. Never call extractfile during registration."""
    with tarfile.open(archive_path, "r:*") as archive:
        members = archive.getmembers()
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        raise ValueError("Conflicting duplicate archive member names")
    headers = [{"name": member.name, "size": member.size, "type": member.type.decode("ascii")}
               for member in sorted(members, key=lambda member: member.name)]
    by_name = {member.name: member for member in members}
    expert, relevant = {}, []
    for member in members:
        if "/aggregated/starting_spans/" in member.name and "/test/" in member.name:
            relevant.append({"name": member.name, "size": member.size, "type": member.type.decode("ascii")})
        match = re.fullmatch(r".+/annotations/aggregated/starting_spans/"
                             r"(participants|interventions|outcomes)/test/gold/(\d+)\.AGGREGATED\.ann", member.name)
        if match:
            if not member.isfile():
                raise ValueError("Expert annotation member must be a regular file")
            task, pmid = match.groups()
            if task in expert.setdefault(pmid, {}):
                raise ValueError("Conflicting expert annotation files for a paper/task")
            expert[pmid][task] = member.name
    if len(expert) != EXPECTED_GOLD_UNION or not set(EXCLUSIONS) <= set(expert):
        raise ValueError("Expert header inventory differs from preregistered official release")
    selected = sorted(set(expert) - set(EXCLUSIONS))
    if len(selected) != EXPECTED_SELECTED:
        raise ValueError("Selected supplement paper count differs from protocol")
    mapping = []
    for pmid in selected:
        text_members = [name for name in names if re.fullmatch(r".+/documents/" + pmid + r"\.(?:txt|text)", name)]
        token_members = [name for name in names if re.fullmatch(r".+/documents/" + pmid + r"\.tokens", name)]
        if len(text_members) != 1 or len(token_members) != 1:
            raise ValueError("Missing or conflicting raw text/tokens member")
        pair = {"pmid": pmid, "text_member": text_members[0], "tokens_member": token_members[0],
                "task_members": dict(sorted(expert[pmid].items()))}
        for name in [pair["text_member"], pair["tokens_member"], *pair["task_members"].values()]:
            if not by_name[name].isfile():
                raise ValueError("Selected source member must be a regular file")
        mapping.append(pair)
    return {"archive_header_inventory_sha256": digest(dumps(headers)), "archive_members": len(headers),
            "header_only_test_inventory": sorted(relevant, key=lambda member: member["name"]),
            "expert_paper_task_members": {pmid: dict(sorted(tasks.items())) for pmid, tasks in sorted(expert.items())},
            "selected_ids": selected, "selected_member_mapping": mapping,
            "selected_papers": len(selected),
            "annotated_papers_by_task": {task: sum(task in expert[pmid] for pmid in selected) for task in TASKS}}


def _frozen_identity(settings, path, engine, archive_sha, crosswalk_path=None):
    path = Path(path).resolve()
    frozen = json.loads(path.read_bytes())
    if (not benchmarks.verify_receipt(settings, "public_benchmark_freeze", frozen)
            or frozen.get("schema_version") != "literature-public-freeze-v1"
            or frozen.get("corpus") != "ebmnlp" or frozen.get("release_sha256") != archive_sha
            or frozen.get("provenance", {}).get("engine_version") != engine):
        raise ValueError("Original EBM engine freeze is missing, altered, or for another release/engine")
    _, current = benchmarks._provenance(settings, engine, crosswalk_path)
    if frozen["provenance"] != current or frozen.get("projection") != benchmarks.PROJECTION:
        raise ValueError("Frozen evaluator/model/rules/dictionary/crosswalk/projection identity changed")
    return {"path": str(path), "sha256": _file_sha(path), "freeze_id": frozen["id"],
            "frozen_at": frozen["frozen_at"], "provenance": frozen["provenance"], "projection": frozen["projection"]}


def _check_exposure(settings, selected, own_derived_archive):
    selected = set(selected)
    for path in sorted((settings.home / "public_benchmarks").glob("*/*/inputs.json")):
        inputs = json.loads(path.read_bytes())
        # Idempotent re-registration may find its own previously prepared test.
        if (inputs.get("corpus") == "ebmnlp" and inputs.get("split") == "test"
                and inputs.get("manifest", {}).get("source_url") == str(own_derived_archive)
                and Path(own_derived_archive).is_file()
                and inputs["manifest"].get("source_sha256") == _file_sha(own_derived_archive)
                and {str(paper["pmid"]) for paper in inputs.get("documents", [])} == selected):
            continue
        overlap = selected & {str(paper["pmid"]) for paper in inputs.get("documents", [])}
        if overlap:
            raise ValueError("Selected supplement papers were already exposed: " + ",".join(sorted(overlap)))
    for path in sorted((settings.home / "evaluation").glob("*/frozen_inputs.json")):
        overlap = selected & {str(paper["pmid"]) for paper in json.loads(path.read_bytes())["documents"]}
        if overlap:
            raise ValueError("Selected supplement papers overlap the nutrition cohort")


def _write_once(path, value):
    path = Path(path)
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError("Immutable supplement record changed: " + str(path))
        return
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(value))


def register_protocol(settings, archive_path, freeze_v1, freeze_v2, output, crosswalk_path=None):
    archive_path, output = Path(archive_path).resolve(), Path(output).resolve()
    archive_sha = _file_sha(archive_path)
    if archive_sha != OFFICIAL_ARCHIVE_SHA256:
        raise ValueError("Official archive SHA-256 differs; no label members have been read")
    inventory = _inventory(archive_path)
    _check_exposure(settings, inventory["selected_ids"], output / "expert-supplement.tar")
    frozen = {engine: _frozen_identity(settings, path, engine, archive_sha, crosswalk_path)
              for engine, path in [("heuristic-v1", freeze_v1), ("heuristic-v2", freeze_v2)]}
    core = {"schema_version": "literature-ebm-expert-supplement-protocol-v1", "version": VERSION,
            "official_source_url": OFFICIAL_URL, "official_archive_path": str(archive_path),
            "official_archive_sha256": archive_sha, "helper_source_sha256": _helper_sha(),
            "output_directory": str(output), "exclusions": EXCLUSIONS, **inventory,
            "frozen_engines": frozen, "evaluator_source_hashes": benchmarks.evaluator_source_hashes(),
            "label_source": "Official expert test/gold only; crowd labels excluded",
            "selection_policy": "Official expert-test union minus four metadata-defined exposure exclusions",
            "path_projection": "expert test/gold/PMID.AGGREGATED.ann -> test/PMID.AGGREGATED.ann; byte-preserving",
            "split_status": "supplementary incomplete official-test subset; not the original complete protocol",
            "original_expert_test_papers": EXPECTED_GOLD_UNION,
            "original_expert_test_coverage_fraction": EXPECTED_SELECTED / EXPECTED_GOLD_UNION,
            "original_expert_test_complete": False,
            "no_algorithm_changes_or_tuning": True,
            "no_label_members_extracted_before_registration": True,
            "crosswalk_path": str(Path(crosswalk_path).resolve()) if crosswalk_path else None}
    protocol_id = digest(dumps(core))[:24]
    path = output / "protocol.json"
    output.mkdir(parents=True, exist_ok=True)
    if path.exists():
        protocol = json.loads(path.read_bytes())
        if {key: value for key, value in protocol.items() if key not in {"id", "registered_at"}} != core:
            raise ValueError("Existing supplement protocol differs from source/header/helper/frozen identity")
        if protocol.get("id") != protocol_id or not benchmarks.verify_receipt(settings, PROTOCOL_KIND, protocol):
            raise ValueError("Supplement protocol lacks an unchanged durable registration receipt")
    else:
        protocol = {**core, "id": protocol_id, "registered_at": now()}
        _write_once(path, protocol)
        benchmarks._receipt(settings, PROTOCOL_KIND, protocol_id, protocol)
    return {"protocol_path": str(path), "protocol": protocol}


def _validated_protocol(settings, protocol_path):
    protocol = json.loads(Path(protocol_path).read_bytes())
    if (protocol.get("version") != VERSION or not benchmarks.verify_receipt(settings, PROTOCOL_KIND, protocol)
            or protocol.get("helper_source_sha256") != _helper_sha()
            or protocol.get("official_archive_sha256") != OFFICIAL_ARCHIVE_SHA256
            or protocol.get("exclusions") != EXCLUSIONS
            or protocol.get("evaluator_source_hashes") != benchmarks.evaluator_source_hashes()):
        raise ValueError("Supplement protocol/source/helper/evaluator receipt changed")
    if _file_sha(protocol["official_archive_path"]) != protocol["official_archive_sha256"]:
        raise ValueError("Official source archive changed after registration")
    inventory = _inventory(protocol["official_archive_path"])
    if any(protocol.get(key) != value for key, value in inventory.items()):
        raise ValueError("Archive header/member mapping changed after registration")
    _check_exposure(settings, protocol["selected_ids"], Path(protocol["output_directory"]) / "expert-supplement.tar")
    for engine, frozen in protocol["frozen_engines"].items():
        current = _frozen_identity(settings, frozen["path"], engine, protocol["official_archive_sha256"],
                                   protocol.get("crosswalk_path"))
        if current != frozen:
            raise ValueError("Original engine freeze changed after supplement registration")
    return protocol


def derive_archive(settings, protocol_path):
    protocol = _validated_protocol(settings, protocol_path)
    output = Path(protocol["output_directory"])
    archive_path, receipt_path = output / "expert-supplement.tar", output / "derivation.json"
    if archive_path.exists() or receipt_path.exists():
        if not archive_path.exists() or not receipt_path.exists():
            raise ValueError("Incomplete supplement derivation; cannot silently replace partial files")
        receipt = json.loads(receipt_path.read_bytes())
        if (not benchmarks.verify_receipt(settings, DERIVATION_KIND, receipt)
                or receipt.get("protocol_sha256") != digest(dumps(protocol))
                or receipt.get("derived_archive_sha256") != _file_sha(archive_path)):
            raise ValueError("Derived archive/receipt differs from audited supplement")
        return {"derived_archive": str(archive_path), "derivation_path": str(receipt_path), "receipt": receipt}
    pairs = []
    for paper in protocol["selected_member_mapping"]:
        pmid = paper["pmid"]
        pairs.extend([(pmid, paper["text_member"], f"{VERSION}/documents/{pmid}.txt", "text"),
                      (pmid, paper["tokens_member"], f"{VERSION}/documents/{pmid}.tokens", "tokens")])
        pairs.extend((pmid, member, f"{VERSION}/annotations/aggregated/starting_spans/{task}/test/{pmid}.AGGREGATED.ann", task)
                     for task, member in sorted(paper["task_members"].items()))
    wanted = {original for _, original, _, _ in pairs}
    selected_bytes = {}
    # The durable, validated protocol exists before the first label extractfile.
    # Stream once: random seeking inside the large gzip release repeatedly
    # decompresses the whole corpus. Only selected member bytes enter memory.
    with tarfile.open(protocol["official_archive_path"], "r|*") as source:
        for member in source:
            if member.name in wanted:
                selected_bytes[member.name] = source.extractfile(member).read()
    if set(selected_bytes) != wanted or _file_sha(protocol["official_archive_path"]) != protocol["official_archive_sha256"]:
        raise ValueError("Source members/source hash changed during derivation")
    copied = []
    with archive_path.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as target:
            for pmid, original, derived, kind in pairs:
                value = selected_bytes[original]
                _add_bytes(target, derived, value)
                copied.append({"pmid": pmid, "kind": kind, "original_member": original,
                               "derived_member": derived, "bytes": len(value),
                               "original_sha256": digest(value), "derived_sha256": digest(value)})
            _add_bytes(target, f"{VERSION}/SUPPLEMENT_PROTOCOL.json", dumps(protocol).encode("utf-8"))
    receipt = {"schema_version": "literature-ebm-expert-supplement-derivation-v1", "id": protocol["id"],
               "derived_at": now(), "protocol_sha256": digest(dumps(protocol)),
               "official_archive_sha256": protocol["official_archive_sha256"],
               "derived_archive_sha256": _file_sha(archive_path), "helper_source_sha256": _helper_sha(),
               "member_hashes": copied, "selected_papers": protocol["selected_papers"],
               "annotated_papers_by_task": protocol["annotated_papers_by_task"]}
    _write_once(receipt_path, receipt)
    benchmarks._receipt(settings, DERIVATION_KIND, receipt["id"], receipt)
    return {"derived_archive": str(archive_path), "derivation_path": str(receipt_path), "receipt": receipt}


def _add_bytes(archive, name, value):
    member = tarfile.TarInfo(name)
    member.size, member.mtime, member.uid, member.gid, member.mode = len(value), 0, 0, 0, 0o644
    member.uname = member.gname = ""
    archive.addfile(member, io.BytesIO(value))


def prepare_supplement(settings, protocol_path):
    derived = derive_archive(settings, protocol_path)
    result = prepare_public(settings, "ebmnlp", split="test", source_path=derived["derived_archive"])
    if result["documents"] != EXPECTED_SELECTED:
        raise ValueError("Frozen preparation did not preserve the preregistered paper cohort")
    manifest = json.loads(Path(result["manifest_path"]).read_bytes())
    if manifest["complete_partition"] or not manifest["official_split"]:
        raise ValueError("Supplement must retain official-source/incomplete-subset flags")
    return {**result, "supplementary": True, "protocol_path": str(Path(protocol_path).resolve()),
            "derived_archive": derived["derived_archive"], "derivation_path": derived["derivation_path"],
            "original_expert_test_papers": EXPECTED_GOLD_UNION,
            "original_expert_test_covered": EXPECTED_SELECTED, "original_expert_test_complete": False,
            "note": "Original EBM protected protocol remains unavailable; no predictions made"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--archive", type=Path, required=True)
    register.add_argument("--freeze-v1", type=Path, required=True)
    register.add_argument("--freeze-v2", type=Path, required=True)
    register.add_argument("--output", type=Path, required=True)
    register.add_argument("--crosswalk", type=Path)
    for command in ["derive", "prepare"]:
        child = sub.add_parser(command)
        child.add_argument("protocol", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.home:
        settings = Settings(args.home, settings.config)
    if args.command == "register":
        result = register_protocol(settings, args.archive, args.freeze_v1, args.freeze_v2, args.output, args.crosswalk)
        result = {key: value for key, value in result.items() if key != "protocol"}
    elif args.command == "derive":
        result = derive_archive(settings, args.protocol)
        result = {key: value for key, value in result.items() if key != "receipt"}
    else:
        result = prepare_supplement(settings, args.protocol)
    print(dumps(result))


if __name__ == "__main__":
    main()
