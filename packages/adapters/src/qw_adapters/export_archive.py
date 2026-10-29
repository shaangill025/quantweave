"""Safe ZIP container for portability exports (T057 increment 1; spec §12 "Archive
import rejects traversal, symlinks, absolute paths, duplicate paths and executable
auto-activation"; T008 review C-10, F-25; R087). Stdlib `zipfile`, in memory: no
filesystem extraction, network or subprocess.
- Layout: `manifest.json` (the canonical `qw_domain.portability.ExportManifest`), then
  each listed file in path order, deflated, regular-file mode 0644, fixed timestamp,
  no extra fields or comment, so one bundle always yields the same bytes.
- Writer: takes only an `ExportBundle`, which `build_export` alone makes (current
  format, open and structurally checked sections), writes it and reads it back
  under the same limits.
- Reader, before anything is decompressed:
  - size and entry count; the archive is exactly local records from offset 0, back to
    back, then the central directory, then a 22-byte end record ending the data (no
    prepended or appended data, gaps, overlaps, comment or ZIP64);
  - per entry: encryption flags, other flags than UTF-8 names and deflate options,
    extra fields, symlinks, directories and other special entries, executable mode
    bits, compression other than stored/deflated, C-10 paths; each local header's
    flags, method, CRC, sizes and name must equal its central-directory entry;
  - duplicate and case-folded names, declared per-entry and total sizes, and the
    compression ratio for entries above `ratio_floor`.
  Each entry is read with a bound (zipfile stops at the declared size and checks the
  CRC), so total inflation is bounded by the declared total. The manifest then
  verifies the entry set and every hash. Every failure is a typed `PortabilityError`.
- `ratio` trades availability for bomb margin: highly repetitive legitimate JSON can
  exceed 100x, so the ratio applies only above `ratio_floor` (1 MiB); the size limits
  bound inflation regardless.
LIMITATIONS: the limits are design defaults, not qualified capacity figures; no
encryption or signature (integrity, not authenticity); zipfile truncates a NUL in a
name before these checks; Windows reserved names (`CON`, `aux.json`) and trailing
dots pass the path rules because nothing is extracted to disk (T058/T060 must check
them before any extraction).
"""

import io
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass

from qw_domain.decimals import safe_repr
from qw_domain.portability import (
    MANIFEST_PATH,
    ExportBundle,
    ImportReport,
    PortabilityError,
    Refusal,
    canonical,
    check_path,
    unique_paths,
    verify_import,
)

MIB = 1024 * 1024
_EPOCH = (1980, 1, 1, 0, 0, 0)
_MODE = (stat.S_IFREG | 0o644) << 16
_ENCRYPTED = 0x01 | 0x40  # traditional and strong encryption flag bits
_ALLOWED_FLAGS = 0x02 | 0x04 | 0x800  # deflate options, UTF-8 name
_DOS_SPECIAL = 0x08 | 0x10  # volume label, directory
_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_LOCAL = struct.Struct("<4sHHHHHIIIHH")  # 30-byte local file header
_END = struct.Struct("<4sHHHHIIH")  # 22-byte end of central directory record
_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError,
                RuntimeError, ValueError)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Limits:
    archive_bytes: int = 64 * MIB
    entries: int = 64
    entry_bytes: int = 16 * MIB
    total_bytes: int = 64 * MIB
    ratio: int = 100  # uncompressed / compressed, per entry above ratio_floor
    ratio_floor: int = MIB


DEFAULT_LIMITS = Limits()


def write_archive(bundle: ExportBundle, limits: Limits = DEFAULT_LIMITS) -> bytes:
    if type(bundle) is not ExportBundle:
        raise PortabilityError(Refusal.FORMAT_NOT_WRITABLE, "use build_export")
    buf = io.BytesIO()
    entries = [(MANIFEST_PATH, canonical(bundle.manifest.to_wire())),
               *sorted(bundle.files.items())]  # fmt: skip
    with zipfile.ZipFile(buf, "w") as z:
        for name, raw in entries:
            info = zipfile.ZipInfo(name, _EPOCH)
            info.external_attr, info.create_system = _MODE, 3
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
    out = buf.getvalue()
    read_archive(out, limits)
    return out


def _refuse(code: Refusal, name: str) -> PortabilityError:
    return PortabilityError(code, safe_repr(name))


def _check_entry(i: zipfile.ZipInfo) -> None:
    mode = i.external_attr >> 16
    if i.flag_bits & _ENCRYPTED:
        raise _refuse(Refusal.ENTRY_ENCRYPTED, i.filename)
    if i.flag_bits & ~_ALLOWED_FLAGS or i.extra:
        raise _refuse(Refusal.ENTRY_UNSUPPORTED, i.filename)
    if stat.S_ISLNK(mode):
        raise _refuse(Refusal.SYMLINK, i.filename)
    if (
        i.is_dir()
        or i.external_attr & _DOS_SPECIAL
        or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))
    ):
        raise _refuse(Refusal.SPECIAL_ENTRY, i.filename)
    if mode & 0o111:
        raise _refuse(Refusal.EXECUTABLE_ENTRY, i.filename)
    if i.compress_type not in _METHODS:
        raise _refuse(Refusal.COMPRESSION_UNSUPPORTED, i.filename)
    check_path(i.filename)


def _check_layout(data: bytes, infos: list[zipfile.ZipInfo]) -> None:
    """Local records from offset 0, back to back, each matching its central entry;
    then the central directory; then the end record, ending the data."""
    layout = PortabilityError(Refusal.ARCHIVE_LAYOUT, "not a canonical single ZIP")
    if len(data) < _END.size:
        raise layout
    sig, disk, cd_disk, n_disk, n_all, cd_size, cd_at, comment = _END.unpack_from(
        data, len(data) - _END.size)  # fmt: skip
    if (sig, disk, cd_disk, comment) != (b"PK\x05\x06", 0, 0, 0) or (
        n_disk != n_all or n_all != len(infos)
        or cd_at + cd_size != len(data) - _END.size
    ):  # fmt: skip
        raise layout
    at = 0
    for i in sorted(infos, key=lambda x: x.header_offset):
        if i.header_offset != at or at + _LOCAL.size > cd_at:
            raise layout
        sig, _, flags, method, _, _, crc, csize, usize, nlen, elen = _LOCAL.unpack_from(
            data, at)  # fmt: skip
        name = data[at + _LOCAL.size : at + _LOCAL.size + nlen]
        if (sig, flags, method, crc, csize, usize, elen) != (
            b"PK\x03\x04", i.flag_bits, i.compress_type, i.CRC, i.compress_size,
            i.file_size, 0,
        ) or name != i.orig_filename.encode("ascii", "replace"):  # fmt: skip
            raise _refuse(Refusal.LOCAL_HEADER_MISMATCH, i.filename)
        at += _LOCAL.size + nlen + csize
    if at != cd_at:
        raise layout


def read_archive(data: bytes, limits: Limits = DEFAULT_LIMITS) -> ImportReport:
    if len(data) > limits.archive_bytes:
        raise PortabilityError(Refusal.ARCHIVE_TOO_LARGE, str(len(data)))
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, EOFError, OSError):
        raise PortabilityError(Refusal.ARCHIVE_MALFORMED, "not a ZIP") from None
    files: dict[str, bytes] = {}
    with z:
        infos = z.infolist()
        if len(infos) > limits.entries:
            raise PortabilityError(Refusal.TOO_MANY_ENTRIES, str(len(infos)))
        total = 0
        for i in infos:
            _check_entry(i)
            if i.file_size > limits.entry_bytes:
                raise _refuse(Refusal.ENTRY_TOO_LARGE, i.filename)
            total += i.file_size
            if total > limits.total_bytes:
                raise PortabilityError(Refusal.TOTAL_TOO_LARGE, str(total))
            if i.file_size > max(limits.ratio_floor,
                                 limits.ratio * i.compress_size):  # fmt: skip
                raise _refuse(Refusal.COMPRESSION_RATIO, i.filename)
        unique_paths([i.filename for i in infos])
        _check_layout(data, infos)
        for i in infos:
            try:
                with z.open(i) as f:
                    raw = f.read(limits.entry_bytes + 1)
            except _READ_ERRORS:
                raise _refuse(Refusal.ARCHIVE_MALFORMED, i.filename) from None
            if len(raw) != i.file_size:
                raise _refuse(Refusal.SIZE_MISMATCH, i.filename)
            files[i.filename] = raw
    manifest_raw = files.pop(MANIFEST_PATH, None)
    if manifest_raw is None:
        raise PortabilityError(Refusal.MANIFEST_MISSING, MANIFEST_PATH)
    return verify_import(manifest_raw, files)
