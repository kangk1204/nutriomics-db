from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path

# Pure utilities only: none of these reads or assigns the core experiment HOME.
from ..evidence_v8.common import (MODEL, REVISION, atomic, canonical, digest, exact,
                                 filehash, gpu_process_allowed, groups, metric, read, seed_all, textkey,
                                 validate_gpu)

ROOT = Path(__file__).resolve().parents[2]
ENGINE = "nutriomics-native-v8"
POLICY = {"engine": ENGINE, "model": MODEL, "revision": REVISION, "seed": 20261023,
          "outer_folds": 5, "inner_folds": 3, "C": [.1, 1., 10.],
          "learning_rates": [1e-5, 2e-5], "epochs": 5, "patience": 2,
          "thresholds": [i/100 for i in range(5, 100, 5)]+[.99],
          "gpu_gib": 13, "rss_gib": 8, "additional_api_calls": 0,
          "cross_task_label_mapping": False, "core_model_transfer": False,
          "automatic_entities_measured": False, "sealed_test_opened": False}


def home():
    return Path(os.environ.get("EVIDENCE_V8_NATIVE_HOME", ROOT/"runtime/nutriomics-native-v8"))


def sources():
    files = [*Path(__file__).parent.glob("*.py"), ROOT/"scripts/evidence_v8_native.py",
             ROOT/"nutri_literature/evidence_v8/common.py"]
    return {str(p.relative_to(ROOT)).replace("\\", "/"): filehash(p) for p in files if p.exists()}


def checkpoint():
    if (home()/"cancel-request.json").exists():
        raise InterruptedError("native 작업 취소: 저장된 epoch/CPU 단계를 보존")


def setup_gpu():
    import psutil
    import torch
    if psutil.virtual_memory().available < 16*1024**3 or shutil.disk_usage(home()).free < 50*1024**3:
        raise RuntimeError("가용 RAM 16GiB·디스크 50GiB 미달: 대기")
    output = subprocess.run(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.free",
                             "--format=csv,noheader,nounits"], check=True, capture_output=True, text=True)
    inventory = [dict(zip(("uuid", "name", "total_mib", "free_mib"), map(str.strip, line.split(","))))
                 for line in output.stdout.splitlines() if line.strip()]
    requested = os.environ.get("EVIDENCE_V8_NATIVE_GPU_UUID")
    if not inventory or (len(inventory) > 1 and not requested):
        raise RuntimeError("가용 GPU 없음 또는 다중 GPU UUID 미지정")
    gpu = next((r for r in inventory if r["uuid"] == requested), None) if requested else inventory[0]
    if gpu is None:
        raise RuntimeError("선택 GPU UUID 없음")
    resource = read(home()/"resource-policy.json") if (home()/"resource-policy.json").exists() else {}
    validate_gpu(gpu, resource.get("forbidden_uuids", []))
    output = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory", "--format=csv,noheader,nounits"],
                            check=True, capture_output=True, text=True)
    for line in output.stdout.splitlines():
        fields = list(map(str.strip, line.split(",")))
        if len(fields) != 3:
            raise RuntimeError("native GPU 프로세스 판독 실패: 대기")
        process = dict(zip(("pid", "gpu_uuid", "used_mib"), fields))
        if not gpu_process_allowed(process, gpu["uuid"], resource):
            raise RuntimeError("선택 GPU의 기존 계산 작업 보존: native 학습 대기")
    os.environ["CUDA_VISIBLE_DEVICES"] = gpu["uuid"]
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 의존성 없음: CPU 성공으로 대체하지 않음")
    torch.set_num_threads(1)
    torch.cuda.set_per_process_memory_fraction(13*1024**3/torch.cuda.get_device_properties(0).total_memory)
    return torch.device("cuda:0"), gpu


class Ledger:
    def __init__(self, stage):
        self.path = home()/"gpu-ledger.json"
        if not self.path.exists():
            atomic(self.path, {"engine": ENGINE, "used_seconds": 0., "active": None, "sessions": []})
        value = read(self.path)
        if value.get("engine") != ENGINE:
            raise ValueError("다른 실험 장부 수정 금지")
        if value.get("active"):
            value["used_seconds"] += max(0., time.time()-value["active"]["heartbeat"])
        value["active"] = {"stage": stage, "pid": os.getpid(), "heartbeat": time.time()}
        self.start = time.monotonic()
        atomic(self.path, value)

    def check(self):
        import psutil
        checkpoint()
        if psutil.Process().memory_info().rss > 8*1024**3:
            raise MemoryError("native 작업 RSS 8GiB 한도 초과")
        value = read(self.path)
        elapsed = max(0., time.time()-value["active"]["heartbeat"])
        if elapsed >= 15:
            value["used_seconds"] += elapsed
            value["active"]["heartbeat"] = time.time()
            atomic(self.path, value)

    def stop(self):
        value = read(self.path)
        value["used_seconds"] += max(0., time.time()-value["active"]["heartbeat"])
        value["sessions"].append({"stage": value["active"]["stage"], "seconds": time.monotonic()-self.start})
        value["active"] = None
        atomic(self.path, value)


class Busy(RuntimeError):
    pass


@contextlib.contextmanager
def owner():
    import psutil
    home().mkdir(parents=True, exist_ok=True)
    path = home()/"owner.json"
    if path.exists():
        old = read(path)
        try:
            if abs(psutil.Process(old["pid"]).create_time()-old["created"]) < 1:
                raise Busy("native 작업 이미 실행 중")
        except psutil.NoSuchProcess:
            pass
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as stream:
        stream.write(canonical({"pid": os.getpid(), "created": psutil.Process().create_time()}))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
