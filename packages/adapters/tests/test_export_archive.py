"""Safe ZIP container for portability exports (T057 increment 1). In memory only. All
archive content is SYNTHETIC; hostile archives are built here with stdlib zipfile and
byte patches (the review round 1 probes).
"""

import io
import stat
import struct
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from qw_adapters.export_archive import (
    DEFAULT_LIMITS,
    MIB,
    Limits,
    read_archive,
    write_archive,
)
from qw_domain import portability as pt

R = pt.Refusal
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
TENANT = "tenant-synth-a"
CAND: list[dict[str, object]] = [
    {"candidate_id": "c-synth-1", "payload": None, "target": "personal_adaptation",
     "verdicts": []}]  # fmt: skip
LINEAGE = [{"node": f"record/{TENANT}/r1", "kind": "record", "reproducible": True}]
CP, LP = "learning/candidates.json", "learning/lineage.json"
CD, LH, END = b"PK\x01\x02", b"PK\x03\x04", b"PK\x05\x06"
Entries = Sequence[tuple[str | zipfile.ZipInfo, bytes]]


def bundle(sections: dict[str, object] | None = None) -> pt.ExportBundle:
    sections = sections or {CP: CAND, LP: LINEAGE}
    return pt.build_export("export-synth-1", TENANT, AT, sections,
                           reconnections=["conn-synth-1"])  # fmt: skip


def parts() -> list[tuple[str | zipfile.ZipInfo, bytes]]:
    b = bundle()
    return [("manifest.json", pt.canonical(b.manifest.to_wire())),
            *sorted(b.files.items())]  # fmt: skip


def raw_zip(entries: Entries, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, raw in entries:
            z.writestr(name, raw)
    return buf.getvalue()


def patch(data: bytes, sig: bytes, off: int, fmt: str, val: int, which: int) -> bytes:
    """Set one field of the `which`-th record with signature `sig`."""
    d, pos = bytearray(data), -1
    for _ in range(which + 1):
        pos = d.index(sig, pos + 1)
    struct.pack_into(fmt, d, pos + off, val)
    return bytes(d)


def refused(data: bytes, limits: Limits = DEFAULT_LIMITS) -> pt.Refusal:
    with pytest.raises(pt.PortabilityError) as e:
        read_archive(data, limits)
    return e.value.code


def test_round_trip_preserves_state_and_reports_omissions() -> None:
    data = write_archive(bundle())
    assert data == write_archive(bundle())  # deterministic bytes
    rep = read_archive(data)
    assert rep.data == {CP: CAND, LP: LINEAGE}
    assert rep.omissions == pt.ALWAYS_OMITTED and rep.unsupported == ()
    assert rep.pending_reconnections == ("conn-synth-1",)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
    assert [i.filename for i in infos] == ["manifest.json", CP, LP]
    assert {i.external_attr >> 16 for i in infos} == {stat.S_IFREG | 0o644}
    assert read_archive(raw_zip(parts(), zipfile.ZIP_STORED)).data == rep.data


def test_ratio_applies_above_the_floor_only() -> None:
    half = [{"node": " " * (MIB // 2)}]
    small = bundle({"research/evidence.json": [], LP: half})
    assert read_archive(write_archive(small)).data[LP] == half
    big = bundle({LP: [{"node": " " * (2 * MIB)}]})
    with pytest.raises(pt.PortabilityError) as e:
        write_archive(big)
    assert e.value.code is R.COMPRESSION_RATIO  # the writer reads its output back
    assert refused(raw_zip([*parts(), ("research/evidence.json",
                                       b"[" + b" " * (2 * MIB) + b"]")])) is (
        R.COMPRESSION_RATIO)  # fmt: skip
    with pytest.raises(pt.PortabilityError) as e:
        write_archive(object())  # type: ignore[arg-type]
    assert e.value.code is R.FORMAT_NOT_WRITABLE


@pytest.mark.parametrize(("name", "expect"), [
    ("../escape.json", R.PATH_TRAVERSAL), ("financial/../../x.json", R.PATH_TRAVERSAL),
    ("/etc/cron.d/x", R.PATH_ABSOLUTE), ("C:/Users/x.json", R.PATH_DRIVE),
    ("financial\\..\\x.json", R.PATH_BACKSLASH), ("financial/", R.SPECIAL_ENTRY),
    ("financial/jou\u0301rnal.json", R.PATH), ("research/extra.json", R.ENTRY_UNLISTED),
    ("Learning/Lineage.json", R.CASE_COLLISION),
])  # fmt: skip
def test_hostile_names_refused(name: str, expect: pt.Refusal) -> None:
    assert refused(raw_zip([*parts(), (name, b"[]")])) is expect


@pytest.mark.parametrize(("mode", "expect"), [
    (stat.S_IFLNK | 0o777, R.SYMLINK), (stat.S_IFCHR | 0o644, R.SPECIAL_ENTRY),
    (stat.S_IFIFO | 0o644, R.SPECIAL_ENTRY), (stat.S_IFREG | 0o755, R.EXECUTABLE_ENTRY),
    (stat.S_IFREG | 0o744, R.EXECUTABLE_ENTRY),
])  # fmt: skip
def test_symlink_special_and_executable_entries_refused(
    mode: int, expect: pt.Refusal
) -> None:
    p = parts()
    hostile = zipfile.ZipInfo(str(p[1][0]), (1980, 1, 1, 0, 0, 0))
    hostile.external_attr, hostile.create_system = mode << 16, 3
    assert refused(raw_zip([p[0], (hostile, p[1][1]), p[2]])) is expect


def test_entry_set_and_version_refused() -> None:
    p = parts()
    with pytest.warns(UserWarning, match="Duplicate name"):
        dup = raw_zip([*p, p[1]])
    assert refused(dup) is R.DUPLICATE_PATH
    assert refused(raw_zip(p[:2])) is R.ENTRY_MISSING
    assert refused(raw_zip(p[1:])) is R.MANIFEST_MISSING
    tampered = (p[1][0], p[1][1].replace(b"c-synth-1", b"c-synth-9"))
    assert refused(raw_zip([p[0], tampered, p[2]])) is R.HASH_MISMATCH
    newer = replace(bundle().manifest, format_version="qw-export-1.2",
                    compatibility_version="qw-export-1.1")  # fmt: skip
    raw = pt.canonical(newer.to_wire())
    assert refused(raw_zip([("manifest.json", raw), *p[1:]])) is R.READER_TOO_OLD


G = raw_zip(parts())
_OFF0 = struct.unpack_from("<I", G, G.index(CD) + 42)[0]
_AT = G.index(CD)
_GAP = patch(G[:_AT] + b"JUNK" + G[_AT:], END, 16, "<I", _AT + 4, 0)


@pytest.mark.parametrize(("data", "expect"), [
    (patch(G, CD, 8, "<H", 0x01, 0), R.ENTRY_ENCRYPTED),
    (patch(G, CD, 8, "<H", 0x40, 1), R.ENTRY_ENCRYPTED),
    (patch(G, CD, 8, "<H", 0x2000, 0), R.ENTRY_UNSUPPORTED),
    (patch(G, CD, 8, "<H", 0x08, 2), R.ENTRY_UNSUPPORTED),
    (patch(G, LH, 6, "<H", 0x01, 0), R.LOCAL_HEADER_MISMATCH),
    (patch(G, LH, 6, "<H", 0x08, 1), R.LOCAL_HEADER_MISMATCH),
    (patch(G, LH, 8, "<H", 0, 0), R.LOCAL_HEADER_MISMATCH),
    (patch(G, LH, 18, "<I", 5, 0), R.LOCAL_HEADER_MISMATCH),
    (patch(G, LH, 22, "<I", 5, 0), R.LOCAL_HEADER_MISMATCH),
    (patch(G, LH, 26, "<H", 3, 0), R.LOCAL_HEADER_MISMATCH),
    (patch(G, LH, 28, "<H", 200, 0), R.LOCAL_HEADER_MISMATCH),
    (G.replace(b"manifest.json", b"mXnifest.json", 1), R.LOCAL_HEADER_MISMATCH),
    (b"MZ" + b"A" * 100 + G, R.ARCHIVE_LAYOUT),
    (G + b"junk", R.ARCHIVE_LAYOUT), (G + G, R.ARCHIVE_LAYOUT),
    (G[:-2] + struct.pack("<H", 5) + b"hello", R.ARCHIVE_LAYOUT),
    (patch(G, CD, 42, "<I", _OFF0, 1), R.ARCHIVE_LAYOUT),  # overlapping entries
    (patch(G, END, 10, "<H", 1, 0), R.ARCHIVE_LAYOUT),
    (patch(G, END, 20, "<H", 5, 0), R.ARCHIVE_LAYOUT),  # comment length, no comment
    (_GAP, R.ARCHIVE_LAYOUT),  # hidden data between the records and the directory
    (b"not a zip", R.ARCHIVE_MALFORMED), (G[: len(G) // 2], R.ARCHIVE_MALFORMED),
    (raw_zip(parts(), zipfile.ZIP_BZIP2), R.COMPRESSION_UNSUPPORTED),
])  # fmt: skip
def test_byte_level_tampering_refused(data: bytes, expect: pt.Refusal) -> None:
    assert refused(data) is expect


def test_zip_bomb_limits() -> None:
    p = parts()
    pairs = raw_zip([*p, ("research/evidence.json", b"[]" * 600)])
    assert refused(pairs, Limits(entry_bytes=1000)) is R.ENTRY_TOO_LARGE
    many = [(f"research/e{i}.json", b"[]") for i in range(70)]
    assert refused(raw_zip([*p, *many])) is R.TOO_MANY_ENTRIES
    total = sum(len(raw) for _, raw in p)
    assert refused(G, Limits(total_bytes=total - 1)) is R.TOTAL_TOO_LARGE
    assert refused(G, Limits(archive_bytes=len(G) - 1)) is R.ARCHIVE_TOO_LARGE
    huge = patch(patch(G, CD, 24, "<I", 0xFFFFFFFF, 0), LH, 22, "<I", 0xFFFFFFFF, 0)
    assert refused(huge) is R.ENTRY_TOO_LARGE


def test_lying_sizes_and_corrupt_bodies_refused() -> None:
    usize = struct.unpack_from("<I", G, G.index(CD) + 24)[0]
    for delta, expect in ((1, R.SIZE_MISMATCH), (-1, R.ARCHIVE_MALFORMED)):
        both = patch(patch(G, CD, 24, "<I", usize + delta, 0), LH, 22, "<I",
                     usize + delta, 0)  # fmt: skip
        assert refused(both) is expect  # local and central agree; the body does not
    stored = bytearray(raw_zip(parts(), zipfile.ZIP_STORED))
    stored[stored.index(b"qw-export-1.0")] = ord("Q")  # body changed, CRC kept
    assert refused(bytes(stored)) is R.ARCHIVE_MALFORMED
