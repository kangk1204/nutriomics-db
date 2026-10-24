"""Explicit v8 dictionary inputs; no legacy HOME or evaluation-derived whitelist."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .common import canonical, digest, filehash, home, read
from ..repair_v6_gpu_r2.knowledge import namekey
from ..repair_v6_gpu_r2.text import definitions, normal

ASSET_PATHS = ("aliases.sqlite3", "knowledge/overlay.sqlite3", "knowledge/manifest.json",
               "knowledge/chemical-categories.json")


@dataclass(frozen=True)
class Categories:
    allowed: frozenset[str]
    sha256: str
    source_sha256: dict


def asset_contract(directory=None):
    """Bind the alias DBs and explicit MeSH categories before any model fitting."""
    directory = Path(directory) if directory is not None else home()
    imported = read(directory/"inputs/import.json")["files"]
    if any(path not in imported for path in ASSET_PATHS):
        raise ValueError("v8 자동 언급 사전의 명시적 입력 해시 계약 누락")
    raw = (directory/"knowledge/chemical-categories.json").read_bytes()
    expected = imported["knowledge/chemical-categories.json"]
    if digest(raw) != expected:
        raise ValueError("v8 MeSH 화학 범주 파일 해시 불일치")
    value = json.loads(raw)
    if (not isinstance(value.get("chemical_ids"), list) or not value["chemical_ids"]
            or any(not isinstance(identifier, str) for identifier in value["chemical_ids"])
            or value.get("selected_using_evaluation_answers") is not False):
        raise ValueError("공식 전체 MeSH 화학 범주 입력 계약 불일치")
    provenance = value.get("source_sha256", {})
    if (not isinstance(provenance, dict) or not provenance
            or any(not re.fullmatch(r"[a-fA-F0-9]{64}", str(v)) for v in provenance.values())):
        raise ValueError("공식 MeSH 범주 생성 원본 해시 없음")
    knowledge_manifest = directory/"knowledge/manifest.json"
    if filehash(knowledge_manifest) != imported["knowledge/manifest.json"]:
        raise ValueError("v8 별칭 지식베이스 manifest 해시 불일치")
    knowledge = read(knowledge_manifest)
    if (knowledge.get("base_sha256") != imported["aliases.sqlite3"]
            or knowledge.get("overlay_sha256") != imported["knowledge/overlay.sqlite3"]):
        raise ValueError("v8 별칭 DB와 공식 manifest의 해시 연결 불일치")
    contract = {"files": {path: imported[path] for path in ASSET_PATHS},
                "chemical_category_sources": provenance,
                "dictionary_rule_sha256": filehash(Path(__file__)), "legacy_home_used": False}
    return contract, Categories(frozenset(value["chemical_ids"]), expected, provenance)


def extract(text, kb, categories):
    """Source-aligned literal/abbreviation union, with an explicit category set."""
    if not isinstance(categories, Categories):
        raise ValueError("검증된 v8 화학 범주 입력을 명시해야 함")
    tokens = list(re.finditer(r"\w+(?:[-\u2010-\u2013]\w+)*", text))
    candidates, abbreviations = {}, definitions(text)
    for i, first in enumerate(tokens):
        for j in range(i, min(len(tokens), i+8)):
            start, end = first.start(), tokens[j].end()
            surface = text[start:end]
            if len(surface) > 120 or re.search(r"[.;:!?\n]", surface):
                break
            if len(surface) < 2:
                continue
            candidates.setdefault(namekey(surface), []).append((start, end, surface))
            expansion = abbreviations.get(normal(surface), set())
            if len(expansion) == 1:
                candidates.setdefault(namekey(next(iter(expansion))), []).append((start, end, surface))
    keys, matches = sorted(candidates), {}
    for database in (kb.base, kb.overlay):
        for begin in range(0, len(keys), 250):
            subset = keys[begin:begin+250]
            if not subset:
                continue
            query = ("SELECT a.norm,a.kind,r.native_id,r.names FROM aliases a JOIN records r ON a.rid=r.rid "
                     "WHERE a.kind=? AND a.norm IN ("+",".join("?"*len(subset))+")")
            rows = (row for kind in ("gene", "chemical") for row in database.execute(query, [kind, *subset]))
            for key, kind, identifier, names in rows:
                if kind == "chemical" and identifier not in categories.allowed:
                    continue
                aliases = json.loads(names)
                exact_names = [name for name in aliases if namekey(name) == key]
                if not exact_names:
                    continue
                for start, end, surface in candidates[key]:
                    abbreviation = key != namekey(surface)
                    if kind == "gene" and not abbreviation and len(re.sub(r"\W", "", surface)) <= 3 and surface not in exact_names:
                        continue
                    matches[(kind, start, end)] = {"kind": kind, "start": start, "end": end, "text": surface,
                        "ner_score": 0., "source": "official-abbreviation-union" if abbreviation else "official-alias-union",
                        "dictionary_match_is_automatic_approval": False,
                        "category_source_sha256": categories.sha256 if kind == "chemical" else None}
    return [matches[key] for key in sorted(matches)]
