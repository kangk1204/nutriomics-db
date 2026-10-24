"""V7 contracts: explicit scientific gates, immutable artifacts, no promotion."""
import hashlib
import json
from pathlib import Path

ENGINE = "heuristic-v7"
SEED = 20261017
TYPES = ("Association", "Bind", "Negative_Correlation", "Positive_Correlation")
DIRECTIONS = ("chemical_to_gene", "gene_to_chemical", "undirected")
C_GRID = (0.1, 1.0, 10.0)
THRESHOLDS = tuple(i / 100 for i in range(5, 100, 5)) + (0.99,)
POLICY = {
    "engine": ENGINE, "seed": SEED, "precision": .85, "recall": .30,
    "identity_precision": .90, "candidate_recall": .80, "id_recall_at_10": .90,
    "outer_folds": 5, "inner_folds": 3, "supported_taxa": [9606, 10090, 10116],
    "cpu_only": True, "resident_gib": 8, "minimum_free_gib": 50,
    "minimum_available_gib": 16, "default_engine": "heuristic-v1",
    "nutrition_accuracy": "unmeasured", "clinical_redesign": False,
    "lora": {"layers": [8, 9, 10, 11], "targets": ["query", "value"],
             "r": 8, "alpha": 16, "dropout": .05, "lr": .0001,
             "epochs": 3, "microbatch": 1, "accumulation": 16,
             "max_tokens": 256, "overlap": 32, "dtype": "float32"},
}


def sha(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def immutable(path, value):
    path = Path(path)
    data = canonical(value).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("Immutable artifact differs: " + str(path))
        return path
    with path.open("xb") as stream:
        stream.write(data)
    return path


def sources():
    root = Path(__file__).parent
    names = ["contract_v7.py", *sorted(p.name for p in root.glob("*_v7.py") if p.name != "contract_v7.py")]
    record = {name: sha((root / name).read_bytes()) for name in names}
    return record, "rules-sha256:" + sha(canonical(record))


def counts(expected, predicted):
    a, b = set(expected), set(predicted)
    tp, fp, fn = len(a & b), len(b - a), len(a - b)
    p, r = tp / (tp + fp) if tp + fp else 0., tp / (tp + fn) if tp + fn else 0.
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0.}


def passes(metric):
    return metric.get("tp", 0) > 0 and metric.get("precision", 0) >= .85 and metric.get("recall", 0) >= .30
