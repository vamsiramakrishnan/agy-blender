"""USDZ archives that are a function of their content.

``UsdUtils.CreateNewUsdzPackage`` resolves a layer's dependencies and lays the archive out as the USDZ
specification requires (stored entries, data aligned to 64 bytes, root layer first). It also stamps every entry
with its source file's modification time, in local time. Two archives of identical content therefore differ,
and with them every hash, manifest and content-addressed decision derived from the archive.

:func:`create_usdz` keeps pxr's packaging and then sets each entry's timestamp to the earliest value the zip
format can hold. Only those four bytes per header change: layout, alignment, order and data are pxr's.
"""

from __future__ import annotations

import struct
from pathlib import Path

from pxr import Sdf, UsdUtils

_EOCD = struct.Struct("<IHHHHIIH")  # end of central directory (without the comment)
_EOCD_SIG, _CENTRAL_SIG, _LOCAL_SIG = 0x06054B50, 0x02014B50, 0x04034B50
# DOS time 00:00:00 and date 1980-01-01, little-endian: the zero point of zip timestamps, no time zone involved.
FIXED_DOS_TIME_DATE = struct.pack("<HH", 0, (0 << 9) | (1 << 5) | 1)


def normalise_timestamps(path: str | Path) -> int:
    """Set every entry's modification time to 1980-01-01 00:00:00 in place. Returns the number of entries."""
    p = Path(path)
    data = bytearray(p.read_bytes())
    at = data.rfind(struct.pack("<I", _EOCD_SIG))
    if at < 0:
        raise ValueError(f"{p}: not a zip archive (no end-of-central-directory record)")
    _, disk, cd_disk, _, entries, _, cd_offset, _ = _EOCD.unpack_from(data, at)
    if disk or cd_disk or entries == 0xFFFF or cd_offset == 0xFFFFFFFF:
        raise ValueError(f"{p}: multi-disk and zip64 archives are not supported")
    pos = cd_offset
    for _ in range(entries):
        if struct.unpack_from("<I", data, pos)[0] != _CENTRAL_SIG:
            raise ValueError(f"{p}: corrupt central directory at byte {pos}")
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, pos + 28)
        local = struct.unpack_from("<I", data, pos + 42)[0]
        if struct.unpack_from("<I", data, local)[0] != _LOCAL_SIG:
            raise ValueError(f"{p}: corrupt local header at byte {local}")
        data[pos + 12:pos + 16] = FIXED_DOS_TIME_DATE
        data[local + 10:local + 14] = FIXED_DOS_TIME_DATE
        pos += 46 + name_len + extra_len + comment_len
    p.write_bytes(data)
    return entries


def create_usdz(layer_path: str | Path, out_path: str | Path) -> bool:
    """Package ``layer_path`` and its dependencies as ``out_path``; False when pxr declines to package it."""
    if not UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(layer_path)), str(out_path)):
        return False
    normalise_timestamps(out_path)
    return True
