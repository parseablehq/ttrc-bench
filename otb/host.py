"""The machine a measurement ran on, and a process's peak memory. Every throughput report carries `describe()`, so
`otb scale-report` can say which numbers were measured on which host."""

from __future__ import annotations

import os
import platform
import resource
import sys


def describe() -> dict:
    pages, page = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "memory_gb": round(pages * page / 1e9, 1),
    }


def peak_rss_bytes(who: int = resource.RUSAGE_SELF) -> int:
    # macOS reports ru_maxrss in bytes, Linux in kilobytes
    rss = resource.getrusage(who).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024
