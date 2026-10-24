"""Process-local resource protection for CPU batches, including large XML inputs."""

from contextlib import contextmanager
import os
import threading


@contextmanager
def memory_budget(gib=8):
    """Linux address-space ceiling plus RSS watchdog; never signal another process."""
    if os.name != "posix":
        yield
        return
    import psutil
    import resource
    import signal

    budget = int(gib * 1024**3)
    process = psutil.Process()
    old = resource.getrlimit(resource.RLIMIT_AS)
    # AS prevents a single oversized allocation from exhausting a shared host.
    # RSS provides the requested resident budget independently of mapped files.
    virtual_ceiling = max(budget, int(process.memory_info().vms) + 512 * 1024**2)
    if old[0] != resource.RLIM_INFINITY:
        virtual_ceiling = min(virtual_ceiling, old[0])
    if old[1] != resource.RLIM_INFINITY:
        virtual_ceiling = min(virtual_ceiling, old[1])
    resource.setrlimit(resource.RLIMIT_AS, (virtual_ceiling, old[1]))
    stopped = threading.Event()

    def monitor():
        while not stopped.wait(0.1):
            if process.memory_info().rss > budget:
                os.write(2, b"V5 resident memory budget exceeded; terminating own batch for safe resume\n")
                os.kill(os.getpid(), signal.SIGTERM)
                return

    thread = threading.Thread(target=monitor, name="v5-memory-budget", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)
        resource.setrlimit(resource.RLIMIT_AS, old)
