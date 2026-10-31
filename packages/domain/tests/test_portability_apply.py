"""The destination apply plan (T057 increment 6, `qw_domain.portability_apply`):
refusals before any write, in the docstring's order, and the policy versions it
lists. Every tenant, policy and answer is SYNTHETIC; the oracle is the archive
itself (`verify_import`) and the domain policy history."""

import uuid
from typing import Any

import pytest
from qw_domain import portability as pt
from qw_domain import portability_apply as pa
from qw_domain import tombstone_wire as tw
from qw_domain.retention import RecordKey
from test_portability_tenant_sections import (
    AT,
    journal,
    journal_rows,
    policy_rows,
    watchlist_rows,
)

R = pt.Refusal
TENANT = "00000000-0000-4000-8000-0000000000a1"  # SYNTHETIC scope (a tenant id)
PID = "00000000-0000-4000-8000-0000000000b1"
EMPTY: dict[str, object] = {
    pt.JOURNAL_PATH: journal([], TENANT), pt.WATCHLISTS_PATH: [],
    pt.ACCOUNTS_PATH: {"accounts": [], "account_revisions": [], "sources": [],
                       "mappings": []},
    pt.TOMBSTONES_PATH: {"ledger": tw.header(TENANT, []), "tombstones": [],
                         "checkpoints": []},
}  # fmt: skip


def policies(pid: str = PID) -> list[dict[str, Any]]:
    return [{**r, "policy_id": pid} for r in policy_rows(TENANT)]


def report(sections: dict[str, object]) -> pt.ImportReport:
    b = pt.build_export("export-synth-1", TENANT, AT, sections)
    return pt.verify_import(pt.canonical(b.manifest.to_wire()), b.files)


def refused(rep: pt.ImportReport, dest: str = TENANT,
            key: RecordKey | None = None) -> pt.Refusal:  # fmt: skip
    with pytest.raises(pt.PortabilityError) as e:
        pa.plan(rep, dest, key)
    return e.value.code


def test_the_plan_lists_each_policy_version_in_archive_order() -> None:
    rows = policies()
    p = pa.plan(report({**EMPTY, pt.POLICIES_PATH: rows}), TENANT, None)
    assert [(r.policy_id, r.version, r.content_hash) for r in p.policies] == [
        (uuid.UUID(PID), r["version"], r["content_hash"]) for r in rows
    ]
    assert [r.draft.content_hash for r in p.policies] == [
        r["content_hash"] for r in rows
    ]  # rebuilt, not copied
    assert list(p.policy_rows) == rows and p.scope_id == TENANT
    assert p.sections == tuple(sorted({*EMPTY, pt.POLICIES_PATH}))
    assert p.omissions == pt.ALWAYS_OMITTED  # nothing adoptable travels
    assert p.required_reconnections == () and p.export_id == "export-synth-1"
    assert pa.plan(report({}), TENANT, None).policies == ()  # an empty archive


def test_another_tenant_is_refused() -> None:
    rep = report({pt.POLICIES_PATH: policies()})
    for dest in ("00000000-0000-4000-8000-0000000000a2", TENANT.upper(), ""):
        assert refused(rep, dest) is R.DESTINATION_SCOPE


def test_any_format_but_the_written_one_is_refused() -> None:
    raw = pt.canonical(policies())
    for fmt, extra in (
        ("qw-export-1.1", {}),
        ("qw-export-1.1", {"research/future.json": b"[]"}),
    ):
        files = {pt.POLICIES_PATH: raw, **extra}
        entries = tuple(pt.FileEntry(p, pt.sha256(b), len(b), pt.SECTIONS.get(
            p, "research")) for p, b in sorted(files.items()))  # fmt: skip
        m = pt.ExportManifest(
            "export-synth-1",
            fmt,
            AT,
            TENANT,
            entries,
            pt.ALWAYS_OMITTED,
            (),
            pt.COMPATIBILITY_VERSION,
        )
        rep = pt.verify_import(pt.canonical(m.to_wire()), files)  # it verifies
        assert refused(rep) is R.APPLY_FORMAT  # but it is not applied


@pytest.mark.parametrize(("path", "body"), [
    (pt.JOURNAL_PATH, journal(journal_rows(TENANT), TENANT)),
    (pt.WATCHLISTS_PATH, watchlist_rows(TENANT)),
])  # fmt: skip
def test_a_section_that_would_be_re_stamped_is_refused(path: str, body: Any) -> None:
    rep = report({pt.POLICIES_PATH: policies(), path: body})
    assert refused(rep) is R.SECTION_NOT_APPLICABLE  # the policies are not written


def test_a_policy_id_that_is_not_a_store_key_is_refused() -> None:
    for pid in ("pol-synth-1", PID.upper()):
        assert refused(report({pt.POLICIES_PATH: policies(pid)})) is R.MALFORMED


def test_emptiness_follows_each_shape() -> None:
    for path, body in EMPTY.items():
        assert pa.is_empty(path, body)
    assert not pa.is_empty(pt.JOURNAL_PATH, journal(journal_rows(TENANT), TENANT))
    assert not pa.is_empty(pt.ACCOUNTS_PATH, {**EMPTY[pt.ACCOUNTS_PATH],  # type: ignore[dict-item]
                                              "sources": [{}]})  # fmt: skip
    assert not pa.is_empty(pt.TOMBSTONES_PATH, {**EMPTY[pt.TOMBSTONES_PATH],  # type: ignore[dict-item]
                                                "checkpoints": [{}]})  # fmt: skip
    assert not pa.is_empty(pt.WATCHLISTS_PATH, [{}])
