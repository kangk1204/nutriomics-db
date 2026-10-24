"""Pinned SapBERT + lexical fusion; identical source-only menus for both modes."""
from __future__ import annotations

import contextlib
import gc
import hashlib
import heapq
import itertools
import json
import math
import sqlite3
from pathlib import Path

from .common import (MODEL, REVISION, SAPBERT, Ledger, atomic, canonical, checkpoint,
                     digest, filehash, gpu_setup, home, read)


def candidate_menu(mention):
    ids = list(dict.fromkeys(str(v) for v in mention.get("candidate_ids", []) if v))[:50]
    previous = mention.get("previous_id")
    if previous and str(previous) not in ids:
        ids = ids[:49] + [str(previous)]
    return [None, *ids]


def resolve_models():
    path = home()/"models.json"
    if path.exists():
        result = read(path)
        if result["encoder"] != {"repository": MODEL, "revision": REVISION}:
            raise ValueError("기본 인코더 고정 revision 불일치")
        return result
    from huggingface_hub import HfApi
    sha = HfApi().model_info(SAPBERT).sha
    if not sha or len(sha) != 40:
        raise ValueError("SapBERT revision 확인 실패")
    result = {"encoder": {"repository": MODEL, "revision": REVISION},
              "sapbert": {"repository": SAPBERT, "revision": sha},
              "generative_model_calls": 0}
    atomic(path, result, True)
    return result


def knowledge():
    # Explicit constructor argument only. The old package's HOME is never assigned.
    from ..repair_v6_gpu_r2.knowledge import Knowledge
    return Knowledge(home=home())


def record_stream(kb):
    """Merge legacy/modern schemas and every alias for an identity, bounded RAM.

    Sorting by the indexed identity key permits a streaming merge, rather than a
    dictionary containing a million records. Neither source nor row order chooses
    which official aliases/descriptions survive.
    """
    def rows(db):
        columns = {r[1]: (r[2] or "").upper() for r in db.execute("PRAGMA table_info(records)")}
        missing = {"kind", "native_id", "names"}-columns.keys()
        if missing:
            raise ValueError("공식 별칭 DB 필수 열 없음: "+", ".join(sorted(missing)))
        description = "COALESCE(description,'')" if "description" in columns else "''"
        taxon = "taxon" if "taxon" in columns else "NULL"
        source = "COALESCE(source,'')" if "source" in columns else "''"
        # The actual legacy and modern schemas both declare native_id TEXT and
        # have an index on (kind,native_id). Preserve that fast path.
        identifier = "native_id" if columns["native_id"] == "TEXT" else "CAST(native_id AS TEXT)"
        query = (f"SELECT kind,{identifier},names,{taxon},{description},{source} FROM records "
                 f"WHERE kind IS NOT NULL AND native_id IS NOT NULL ORDER BY kind,{identifier}")
        for kind, identifier, names, taxon, description, source in db.execute(query):
            values = json.loads(names) if isinstance(names, str) else names
            if not isinstance(values, list) or any(not isinstance(n, str) for n in values):
                raise ValueError("공식 별칭 이름은 JSON 문자열 목록이어야 함")
            yield {"kind": str(kind), "native_id": str(identifier), "names": values,
                   "taxon": int(taxon) if taxon is not None else None,
                   "description": str(description or "").strip(), "source": str(source or "").strip()}
    key = lambda r: (r["kind"], r["native_id"])
    merged = heapq.merge(rows(kb.base), rows(kb.overlay), key=key)
    for (kind, identifier), records in itertools.groupby(merged, key=key):
        names, descriptions, taxa, source_names, count = set(), set(), set(), set(), 0
        for record in records:
            count += 1
            names.update(n.strip() for n in record["names"] if n.strip())
            if record["description"]:
                descriptions.add(record["description"])
            if record["taxon"] is not None:
                taxa.add(record["taxon"])
            if record["source"]:
                source_names.add(record["source"])
        stable = lambda s: (s.casefold(), s)
        yield {"kind": kind, "native_id": identifier, "names": sorted(names, key=stable),
               "taxon": next(iter(taxa)) if len(taxa) == 1 else None,
               "taxa": sorted(taxa), "taxon_conflict": len(taxa) > 1,
               "description": "; ".join(sorted(descriptions, key=stable)),
               "sources": sorted(source_names, key=stable), "source_records": count}


def record_schema_probe(kb):
    """CPU-only read probe for the real source files; no labels or DB writes."""
    result = {"schemas": {name: [{"name": r[1], "type": r[2]} for r in db.execute("PRAGMA table_info(records)")]
                           for name, db in (("base", kb.base), ("overlay", kb.overlay))},
              "source_rows": {name: db.execute("SELECT count(*) FROM records").fetchone()[0]
                              for name, db in (("base", kb.base), ("overlay", kb.overlay))},
              "merged_identities": 0, "multiple_source_records": 0,
              "with_description": 0, "taxon_conflicts": 0, "gold_labels_read": False,
              "source_databases_modified": False}
    output_hash = hashlib.sha256()
    for record in record_stream(kb):
        result["merged_identities"] += 1
        result["multiple_source_records"] += record["source_records"] > 1
        result["with_description"] += bool(record["description"])
        result["taxon_conflicts"] += record["taxon_conflict"]
        output_hash.update((canonical(record)+"\n").encode("utf8"))
        if (record["kind"], record["native_id"]) == ("gene", "7157"):
            result["TP53_example"] = record
    result["merged_records_sha256"] = output_hash.hexdigest()
    return result


def candidate_description(kb, kind, identifier, mention):
    if identifier is None:
        return "No supported identity; leave this mention unlinked."
    record = kb.by_id(kind, str(identifier))
    if record is None:
        return kind + "; identity description unavailable"
    return kb.candidate_text(record, mention)


def build_index():
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    path = home()/"retrieval/manifest.json"
    binding = resolve_models()["sapbert"]
    if path.exists():
        value = read(path)
        for relative, expected in value["files"].items():
            if filehash(home()/relative) != expected:
                raise ValueError("SapBERT 색인 해시 불일치")
        if value["model"] != binding:
            raise ValueError("색인·SapBERT revision 불일치")
        if value.get("retrieval_code_sha256") != filehash(Path(__file__)):
            raise ValueError("별칭 병합/색인 구현이 변경됨: 새 실험 색인 필요")
        return value
    dev, gpu = gpu_setup()
    ledger = Ledger()
    ledger.start("sapbert-index")
    tok = AutoTokenizer.from_pretrained(binding["repository"], revision=binding["revision"], cache_dir=home()/"cache/huggingface")
    model = AutoModel.from_pretrained(binding["repository"], revision=binding["revision"], cache_dir=home()/"cache/huggingface").to(dev).eval()
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    kb = knowledge()
    count = sum(1 for _ in record_stream(kb))
    vectors = np.lib.format.open_memmap(directory/"vectors.part.npy", mode="w+", dtype=np.float16,
                                      shape=(count, model.config.hidden_size))
    meta = directory/"rows.part.sqlite3"
    meta.unlink(missing_ok=True)
    db = sqlite3.connect(meta)
    db.execute("CREATE TABLE rows(n INTEGER PRIMARY KEY,kind TEXT,identifier TEXT,taxon INTEGER)")
    db.execute("CREATE INDEX kind_idx ON rows(kind,n)")
    batch, at = [], 0
    def flush(batch, at):
        # Both sources' names and descriptions are available before truncation.
        names = ["; ".join(sorted(r["names"], key=lambda v: (len(v), v.casefold(), v))[:8])
                 + ("; "+r["description"] if r["description"] else "") for r in batch]
        enc = tok(names, padding=True, truncation=True, max_length=128, return_tensors="pt").to(dev)
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            v = torch.nn.functional.normalize(model(**enc).last_hidden_state[:, 0].float(), dim=-1)
        vectors[at:at+len(batch)] = v.cpu().numpy()
        db.executemany("INSERT INTO rows VALUES(?,?,?,?)", [(at+i, r["kind"], r["native_id"], r["taxon"]) for i, r in enumerate(batch)])
        ledger.check()
        return at+len(batch)
    try:
        for record in record_stream(kb):
            batch.append(record)
            if len(batch) == 32:
                at = flush(batch, at)
                batch = []
                if at % 4096 == 0:
                    db.commit()
        if batch:
            at = flush(batch, at)
        if at != count:
            raise ValueError("색인 행수 불일치")
        db.commit()
        db.close()
        vectors.flush()
        del vectors
        (directory/"vectors.part.npy").replace(directory/"vectors.npy")
        meta.replace(directory/"rows.sqlite3")
        result = {"model": binding, "rows": count, "dimensions": model.config.hidden_size,
                  "retrieval_code_sha256": filehash(Path(__file__)),
                  "identity_merge": "streamed union of official names/descriptions; no source or row-order precedence",
                  "knowledge_sha256": filehash(home()/"knowledge/manifest.json"),
                  "uses_gold": False, "gpu_uuid": gpu["uuid"], "files": {
                      str(p.relative_to(home())).replace("\\", "/"): filehash(p)
                      for p in (directory/"vectors.npy", directory/"rows.sqlite3")}}
        atomic(path, result, True)
        return result
    finally:
        with contextlib.suppress(Exception):
            db.close()
        kb.close()
        ledger.stop()
        del model
        gc.collect()
        torch.cuda.empty_cache()


class Retriever:
    def __init__(self):
        import numpy as np
        import torch
        from transformers import AutoModel, AutoTokenizer
        manifest = build_index()
        binding = manifest["model"]
        self.dev, self.gpu = gpu_setup()
        self.tok = AutoTokenizer.from_pretrained(binding["repository"], revision=binding["revision"], cache_dir=home()/"cache/huggingface")
        self.model = AutoModel.from_pretrained(binding["repository"], revision=binding["revision"], cache_dir=home()/"cache/huggingface").to(self.dev).eval()
        self.array = np.load(home()/"retrieval/vectors.npy", mmap_mode="r")
        self.db = sqlite3.connect((home()/"retrieval/rows.sqlite3").resolve().as_uri()+"?mode=ro&immutable=1", uri=True)
        # Index vectors are small enough for the permitted GPU. Copy once, not
        # hundreds of MB over PCIe for each of tens of thousands of mentions.
        all_vectors = torch.tensor(np.asarray(self.array), device=self.dev, dtype=torch.float16)
        self.kind_ids, self.kind_vectors = {}, {}
        for kind in ("gene", "chemical"):
            records = self.db.execute("SELECT n,identifier FROM rows WHERE kind=? ORDER BY n", (kind,)).fetchall()
            self.kind_ids[kind] = [identifier for _, identifier in records]
            indices = torch.tensor([n for n, _ in records], device=self.dev)
            self.kind_vectors[kind] = all_vectors[indices]
        del all_vectors
        self.kb = knowledge()
        self.ledger = Ledger()
        self.ledger.start("sapbert-queries")
        self.cache = {}

    def query(self, kind, text):
        import numpy as np
        import torch
        key = (kind, text)
        if key in self.cache:
            return self.cache[key]
        enc = self.tok([text], truncation=True, max_length=128, return_tensors="pt").to(self.dev)
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            q = torch.nn.functional.normalize(self.model(**enc).last_hidden_state[:, 0].float(), dim=-1)[0]
        vectors = self.kind_vectors[kind]
        scores = (vectors @ q.to(torch.float16)).float()
        values, indices = scores.topk(min(200, len(scores)))
        result = sorted([(self.kind_ids[kind][i], float(score)) for i, score in zip(indices.cpu().tolist(), values.cpu().tolist())],
                        key=lambda v: (-v[1], v[0]))
        self.ledger.check()
        if len(self.cache) > 10000:
            self.cache.clear()
        self.cache[key] = result
        return result

    def enrich(self, d, m):
        from ..repair_v6_gpu_r2.text import definitions, normal
        expansions = tuple(sorted(definitions(d["text"]).get(normal(m["text"]), set())))
        lexical = list(self.kb.lexical(m["kind"], m["text"], expansions))
        semantic = self.query(m["kind"], m["text"])
        lr = {v[0]: i for i, v in enumerate(lexical)}
        sr = {v[0]: i for i, v in enumerate(semantic)}
        old = m.get("previous_id")
        exact = {v[0] for v in lexical if v[1]}
        def rank(identifier):
            score = sum(1/(60+ranks[identifier]) for ranks in (lr, sr) if identifier in ranks)
            return (identifier not in exact, -score, identifier)
        ids = sorted(set(lr)|set(sr)|({old} if old else set()), key=rank)[:200]
        if old and old not in ids:
            ids = ids[:199]+[old]
        return {**m, "candidate_ids": ids, "lexical_ids": list(lr), "sapbert_ids": list(sr),
                "retrieval_uses_gold": False, "retrieval_manifest_sha256": filehash(home()/"retrieval/manifest.json")}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        import torch
        self.ledger.stop()
        self.kb.close()
        self.db.close()
        del self.model, self.array, self.kind_vectors
        gc.collect()
        torch.cuda.empty_cache()
