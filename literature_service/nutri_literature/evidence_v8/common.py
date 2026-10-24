from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import subprocess
import time
import unicodedata
from pathlib import Path

ENGINE = "nutriomics-evidence-v8"
ROOT = Path(__file__).resolve().parents[2]
MODEL = "microsoft/BiomedNLP-BiomedBERT-base-uncased-abstract-fulltext"
REVISION = "e1354b7a3a09615f6aba48dfad4b7a613eef7062"
SAPBERT = "cambridgeltl/SapBERT-from-PubMedBERT-fulltext"
TYPES = ("Association", "Bind", "Negative_Correlation", "Positive_Correlation",
         "Comparison", "Conversion", "Cotreatment", "Drug_Interaction")
POLICY = {
    "engine": ENGINE, "model": MODEL, "revision": REVISION,
    "seeds": [20261023, 20261024, 20261025], "outer_folds": 5, "inner_folds": 3,
    "C": [.1, 1., 10.], "learning_rates": [1e-5, 2e-5], "epochs": 5,
    "patience": 2, "retrieval_limit": 200, "menu_limit": 50,
    "thresholds": [i / 100 for i in range(5, 100, 5)] + [.99],
    "id_minimum": 50, "id_wilson_lower": .9, "precision": .85,
    "recall": 65 / 154, "tp": 65, "gpu_gib": 13, "rss_gib": 8,
    "max_views": 8, "dapt_tokens": 50_000_000, "dapt_epochs": 1,
    "weak_label_weight": .1, "supported_taxa": [9606, 10090, 10116],
    "additional_api_calls": 0, "default_engine": "heuristic-v1",
    "sealed_test_opened": False, "nutrition_accuracy": "unmeasured",
}


def home() -> Path:
    return Path(os.environ.get("EVIDENCE_V8_HOME", ROOT / "runtime/nutriomics-evidence-v8"))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_chunks(value):
    """Identical canonical bytes without a second multi-gigabyte JSON buffer."""
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    pending, size = [], 0
    for chunk in encoder.iterencode(value):
        raw = chunk.encode('utf8')
        pending.append(raw)
        size += len(raw)
        if size >= 65536:
            yield b''.join(pending)
            pending, size = [], 0
    if pending:
        yield b''.join(pending)


def canonical_digest(value):
    result = hashlib.sha256()
    for chunk in canonical_chunks(value):
        result.update(chunk)
    return result.hexdigest()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf8")).hexdigest()


def filehash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def textkey(text):
    return digest(" ".join(unicodedata.normalize("NFKC", text).casefold().split()))


def read(path):
    # ijson parses binary input incrementally; json.load first materializes the
    # entire Unicode document (several GiB for full-text mention profiles).
    import ijson
    import sys
    texts = {}
    identifier_suffixes = tuple('.'+key+'.item' for key in
        ('candidate_ids', 'lexical_ids', 'sapbert_ids', 'menu', 'gold_ids'))
    class SharedTextMap(dict):
        def __setitem__(self, key, value):
            # A single paper profile repeats the same identifiers in several
            # menus and across thousands of mentions. Share immutable strings,
            # including across separately loaded fold profiles, without changing
            # any values, ordering, NIL entries, or candidate coverage.
            if key in {'previous_id', 'selected_id', 'source_sha256'} and isinstance(value, str):
                value = sys.intern(value)
            super().__setitem__(key, value)
            if key in ('pmid', 'text') and 'pmid' in self and isinstance(self.get('text'), str):
                text = self['text']
                super().__setitem__('text', texts.setdefault(text, text))
    with Path(path).open('rb') as stream:
        def events():
            # Lists are attached to their map before ijson fills them. Intern
            # their string events instead of replacing an unfinished list.
            for prefix, event, value in ijson.parse(stream, use_float=True):
                if event == 'string' and prefix.endswith(identifier_suffixes):
                    value = sys.intern(value)
                yield prefix, event, value
        values = ijson.items(events(), '', map_type=SharedTextMap)
        sentinel = object()
        value = next(values, sentinel)
        if value is sentinel:
            raise ValueError('Empty JSON artifact')
        if next(values, sentinel) is not sentinel:
            raise ValueError('Multiple JSON artifacts')
        return value


def atomic(path, value, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if filehash(path) != canonical_digest(value):
            raise ValueError("불변 실험 파일 변경: " + str(path))
        return path
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("xb") as stream:
            for chunk in canonical_chunks(value):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def sources():
    files = [*Path(__file__).parent.glob("*.py"), ROOT / "scripts/evidence_v8.py",
             ROOT / "scripts/bootstrap_evidence_v8.py", ROOT / "requirements-evidence-v8.txt"]
    files += [ROOT/"nutri_literature"/name for name in
              ("repair_v6_gpu_r2/text.py", "repair_v6_gpu_r2/knowledge.py", "repair_v6_gpu_r2/neural.py",
               "clinical_v4.py", "text.py")]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): filehash(p) for p in files if p.exists()}


def exact(text, span):
    return (0 <= span["start"] < span["end"] <= len(text)
            and text[span["start"]:span["end"]] == span["text"])


def wilson(ok, n):
    if not n:
        return 0.
    z = 1.959963984540054
    p = ok / n
    return (p + z*z/(2*n) - z*((p*(1-p) + z*z/(4*n))/n)**.5) / (1 + z*z/n)


def metric(expected, predicted):
    expected, predicted = set(expected), set(predicted)
    tp, fp, fn = len(expected & predicted), len(predicted - expected), len(expected - predicted)
    p = tp / (tp + fp) if tp + fp else 0.
    r = tp / (tp + fn) if tp + fn else 0.
    return dict(tp=tp, fp=fp, fn=fn, precision=p, recall=r,
                f1=2*p*r/(p+r) if p+r else 0.)


def seed_all(seed):
    random.seed(seed)
    import numpy as np
    import torch
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def groups(documents, n, salt=""):
    """Connected PMID/PMCID/text/study-family components never cross a fold."""
    parent = list(range(len(documents)))
    seen = {}
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, d in enumerate(documents):
        keys = [("pmid", str(d["pmid"])), ("text", textkey(d["text"]))]
        keys += [(key, str(d[key])) for key in ("pmcid", "study_family") if d.get(key)]
        for key in keys:
            if key in seen:
                parent[find(i)] = find(seen[key])
            else:
                seen[key] = i
    components = {}
    for i, d in enumerate(documents):
        components.setdefault(find(i), []).append(str(d["pmid"]))
    loads, output = [0]*n, {}
    for group in sorted(components.values(), key=lambda g: (-len(g), digest(salt + canonical(sorted(g))))):
        fold = min(range(n), key=lambda j: loads[j])
        loads[fold] += len(group)
        output.update({p: fold for p in group})
    if sum(bool(v) for v in loads) < n:
        raise ValueError(f"{n}-fold 검증을 위한 독립 문헌 그룹 부족")
    return output


def checkpoint():
    if (home() / "cancel-request.json").exists():
        raise InterruptedError("취소 요청: 저장된 단계/epoch 체크포인트 보존")


def gpu_inventory():
    result = subprocess.run(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.free",
                             "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True)
    return [dict(zip(("uuid", "name", "total_mib", "free_mib"), map(str.strip, line.split(","))))
            for line in result.stdout.splitlines() if line.strip()]


def validate_gpu(record, forbidden_uuids=()):
    if any(name in record["name"].upper() for name in ("H200", "A6000")):
        raise RuntimeError("사용자 제외 GPU: " + record["name"])
    if record["uuid"] in forbidden_uuids:
        raise RuntimeError("배정 금지 GPU UUID")
    if int(record["total_mib"]) < 12 * 1024 or int(record["free_mib"]) < 10 * 1024:
        raise RuntimeError("GPU 가용 메모리 부족: 대기 필요")


def gpu_process_allowed(process, selected_uuid, resource_policy, current_pid=None,
                        executable_lookup=None, resolve_path=None):
    """Only an explicit, verified small desktop process can coexist with training."""
    if process["gpu_uuid"] != selected_uuid or int(process["pid"]) == (current_pid or os.getpid()):
        return True
    import psutil
    executable_lookup = executable_lookup or (lambda pid: psutil.Process(pid).exe())
    resolve_path = resolve_path or os.path.realpath
    # This is intentionally narrow; a tiny Python process is still a compute job.
    known_desktop_executables = {"/usr/bin/ptyxis"}
    for entry in resource_policy.get("desktop_allowlist", []):
        executable = entry.get("executable")
        maximum = entry.get("maximum_mib")
        if executable not in known_desktop_executables or not isinstance(maximum, (int, float)) or not 0 < maximum <= 128:
            continue
        try:
            memory = float(process["used_mib"])
            actual_executable = executable_lookup(int(process["pid"]))
            if 0 <= memory <= maximum and resolve_path(actual_executable) == resolve_path(executable):
                return True
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
    return False


def gpu_setup():
    import torch
    import psutil
    import shutil
    if psutil.virtual_memory().available < 16*1024**3:
        raise RuntimeError("학습 시작 가용 RAM 16GiB 미달: 대기 필요")
    if shutil.disk_usage(home()).free < 50*1024**3:
        raise RuntimeError("학습 시작 디스크 여유 50GiB 미달: 대기 필요")
    records = gpu_inventory()
    if not records:
        raise RuntimeError("가용 NVIDIA GPU 없음: 대기 필요")
    requested = os.environ.get("EVIDENCE_V8_GPU_UUID")
    if len(records) != 1 and not requested:
        raise RuntimeError("다중 GPU 서버는 EVIDENCE_V8_GPU_UUID 명시 필요")
    record = next((r for r in records if r["uuid"] == requested), None) if requested else records[0]
    if record is None:
        raise RuntimeError("요청한 GPU UUID 없음")
    resource_policy = read(home()/"resource-policy.json") if (home()/"resource-policy.json").exists() else {}
    forbidden = resource_policy.get("forbidden_uuids", [])
    validate_gpu(record, forbidden)
    active = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory",
                             "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True)
    for line in active.stdout.splitlines():
        fields = list(map(str.strip, line.split(",")))
        if len(fields) != 3:
            raise RuntimeError("GPU 프로세스 정보 판독 실패: 대기 필요")
        process = dict(zip(("pid", "gpu_uuid", "used_mib"), fields))
        if not gpu_process_allowed(process, record["uuid"], resource_policy):
            raise RuntimeError("GPU에서 다른 작업 실행 중: 기존 작업 보존 후 대기 필요")
    # Set this before the first CUDA operation in this process.
    os.environ["CUDA_VISIBLE_DEVICES"] = record["uuid"]
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA/패키지 누락: CPU 실행 성공으로 대체하지 않음")
    torch.set_num_threads(1)
    torch.cuda.set_per_process_memory_fraction(POLICY["gpu_gib"]*1024**3 / torch.cuda.get_device_properties(0).total_memory)
    return torch.device("cuda:0"), record


class Ledger:
    """Own GPU ledger only; a v8 process never edits the r2 ledger."""
    def __init__(self):
        self.path = home()/"gpu-ledger.json"
        self.started = None
        if not self.path.exists():
            atomic(self.path, {"engine": ENGINE, "used_seconds": 0., "sessions": [], "active": None})
        if read(self.path).get("engine") != ENGINE:
            raise ValueError("다른 실험의 GPU 장부 사용 금지")

    def start(self, stage):
        state = read(self.path)
        if state.get("active"):
            # Charge unobserved crash time conservatively, never erase old use.
            state["used_seconds"] += max(0., time.time() - state["active"]["heartbeat"])
        self.started = time.monotonic()
        state["active"] = {"stage": stage, "pid": os.getpid(), "heartbeat": time.time()}
        atomic(self.path, state)

    def check(self):
        import psutil
        checkpoint()
        if psutil.Process().memory_info().rss > POLICY["rss_gib"]*1024**3:
            raise MemoryError("v8 작업 RSS 8GiB 한도 초과")
        if self.started is not None:
            state = read(self.path)
            elapsed = max(0., time.time() - state["active"]["heartbeat"])
            if elapsed >= 15:
                state["used_seconds"] += elapsed
                state["active"]["heartbeat"] = time.time()
                atomic(self.path, state)

    def stop(self):
        if self.started is not None:
            state = read(self.path)
            state["used_seconds"] += max(0., time.time() - state["active"]["heartbeat"])
            state["sessions"].append({"stage": state["active"]["stage"], "seconds": time.monotonic()-self.started})
            state["active"] = None
            atomic(self.path, state)
            self.started = None


class OwnerBusy(RuntimeError):
    pass


@contextlib.contextmanager
def owner():
    import psutil
    home().mkdir(parents=True, exist_ok=True)
    path = home()/"owner.json"
    value = {"pid": os.getpid(), "created": psutil.Process().create_time(), "started": time.time()}
    if path.exists():
        old = read(path)
        try:
            if abs(psutil.Process(old["pid"]).create_time()-old["created"]) < 1:
                raise OwnerBusy("동일 v8 실험 이미 실행 중")
        except psutil.NoSuchProcess:
            pass
        path.unlink()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf8") as stream:
        stream.write(canonical(value))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
