"""Publish a fully validated package on the same filesystem as its staging directory."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


def publish_directory(candidate: Path, destination: Path) -> None:
    """Atomically install candidate. On exchange the prior directory is left at candidate.

    Readers of an existing package see either complete generation. A filesystem without
    a directory-exchange primitive preserves the old package and reports the limitation.
    The caller owns the staging directory, including cleanup of any exchanged generation.
    """
    try:
        candidate.rename(destination)
        return
    except OSError as exc:
        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
            raise
    libc = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(candidate), os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, old, -100, new, 2)  # AT_FDCWD, RENAME_EXCHANGE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(old, new, 2)  # RENAME_SWAP
    else:
        raise OSError(errno.ENOTSUP, "atomic directory replacement unavailable", destination)
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)
