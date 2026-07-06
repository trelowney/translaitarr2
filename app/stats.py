"""Lightweight container CPU / RAM stats from cgroup (v2, with v1 fallbacks).

Dependency-free. CPU% is the busy time between two calls over wall-clock — so it
reflects the poll interval, and can exceed 100% on multi-core work.
"""
import time

_last = {"usage": None, "t": None}


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().split()[0])
    except (OSError, ValueError):
        return None


def _cpu_usage_usec():
    try:  # cgroup v2
        with open("/sys/fs/cgroup/cpu.stat") as f:
            for line in f:
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
    except OSError:
        pass
    ns = (_read_int("/sys/fs/cgroup/cpuacct/cpuacct.usage")
          or _read_int("/sys/fs/cgroup/cpu/cpuacct.usage"))  # cgroup v1 (ns)
    return ns // 1000 if ns is not None else None


def _inactive_file_bytes():
    # Reclaimable page cache — subtract it so we report the working set, not
    # the kernel's file cache of media/subtitles (matches `docker stats`).
    for path, key in (("/sys/fs/cgroup/memory.stat", "inactive_file"),
                      ("/sys/fs/cgroup/memory/memory.stat", "total_inactive_file")):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith(key + " "):
                        return int(line.split()[1])
        except OSError:
            continue
    return 0


def container_stats():
    mem = (_read_int("/sys/fs/cgroup/memory.current")
           or _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes"))
    if mem:
        mem = max(mem - (_inactive_file_bytes() or 0), 0)
    usage = _cpu_usage_usec()
    now = time.time()
    cpu_pct = None
    if usage is not None and _last["usage"] is not None and _last["t"] is not None:
        dt = now - _last["t"]
        if dt > 0:
            cpu_pct = round((usage - _last["usage"]) / 1e6 / dt * 100, 1)
    _last["usage"], _last["t"] = usage, now
    return {"ram_mb": round(mem / 1048576, 1) if mem else None, "cpu_pct": cpu_pct}
