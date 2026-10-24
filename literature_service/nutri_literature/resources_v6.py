"""8-GiB resident watchdog with a separate bounded model-mapping allowance."""

from contextlib import contextmanager
import os
import threading

POLICY = {"resident_gib": 8, "virtual_gib": 16, "sampling_seconds": 0.1, "terminate_own_process_only": True}


def virtual_limit(gib, old_soft, old_hard):
    limit = int(gib * 1024**3)
    for inherited in (old_soft, old_hard):
        if inherited >= 0:
            limit = min(limit, inherited)
    return limit


@contextmanager
def memory_budget(gib=8):
    if gib <= 0 or gib > POLICY["resident_gib"]:
        raise ValueError("V6 resident budget must be positive and at most 8 GiB")
    record = {"resident_budget_bytes": int(gib * 1024**3), "peak_rss_bytes": None}
    if os.name != "posix":
        yield record
        return
    import psutil
    import resource
    import signal

    process = psutil.Process()
    old = resource.getrlimit(resource.RLIMIT_AS)
    ceiling = virtual_limit(min(POLICY["virtual_gib"], gib * 2), *old)
    if process.memory_info().vms >= ceiling:
        raise MemoryError("Inherited address-space allowance is already exhausted")
    record.update(virtual_ceiling_bytes=ceiling, peak_rss_bytes=process.memory_info().rss)
    resource.setrlimit(resource.RLIMIT_AS, (ceiling, old[1]))
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(POLICY["sampling_seconds"]):
            rss = process.memory_info().rss
            record["peak_rss_bytes"] = max(record["peak_rss_bytes"], rss)
            if rss > record["resident_budget_bytes"]:
                os.write(2, b"V6 8-GiB resident budget exceeded; stopping own resumable batch\n")
                os.kill(os.getpid(), signal.SIGTERM)
                return

    thread = threading.Thread(target=monitor, name="v6-resident-budget", daemon=True)
    thread.start()
    try:
        yield record
    finally:
        stopped.set()
        thread.join(timeout=1)
        record["peak_rss_bytes"] = max(record["peak_rss_bytes"], process.memory_info().rss)
        resource.setrlimit(resource.RLIMIT_AS, old)
