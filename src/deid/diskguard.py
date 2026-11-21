"""Stop safely before a disk fills up. Used by the transform loop (and by scripts/10_download.sh via `python -m deid freespace`)."""
from __future__ import annotations

import os
import shutil


class DiskLow(RuntimeError):
    """Raised when a guarded filesystem has less free space than the configured minimum."""


def free_gib(path: str) -> float:
    return shutil.disk_usage(path).free / (1 << 30)


def default_paths(output_dir: str) -> list:
    """The output filesystem plus any host drives named in DEID_GUARD_PATHS (colon separated, e.g. /mnt/c).

    Inside WSL2 the virtual disk can grow until the *Windows* drive is full, so the host drive is guarded too."""
    paths = [output_dir]
    for p in os.environ.get("DEID_GUARD_PATHS", "").split(":"):
        if p.strip():
            paths.append(p.strip())
    return paths


def check(paths, min_free_gib: float) -> None:
    for p in paths:
        if p and os.path.exists(p):
            f = free_gib(p)
            if f < min_free_gib:
                raise DiskLow(f"{p}: only {f:.1f} GiB free (minimum {min_free_gib:g} GiB)")
