"""The destination apply plan (T057 increments 6 and 7, `qw_domain.portability_apply`):
refusals before any write, in the docstring's order, and the policy versions it
lists. Every tenant, policy and answer is SYNTHETIC; the oracle is the archive
itself (`verify_import`) and the domain policy history."""

import json
import uuid
from datetime import UTC, datetime
from functools import partial
from typing import Any

import pytest
from qw_domain import account_wire as aw
from qw_domain import portability as pt
from qw_domain import portability_apply as pa
from qw_domain import tombstone_wire as tw
from qw_domain.instants import format_instant
from qw_domain.retention import RecordKey
from qw_domain.sources import MappedInterval, Source, SourceKind, UnderlyingAccount
from test_learned_state import exported
from test_portability_tenant_sections import (
    AT,
    journal,
    journal_rows,
    policy_rows,
    watchlist_rows,
)
from test_trial_ledger import TENANT as LS_TENANT  # the learned state's tenant

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


V0, V1 = datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 6, 1, tzinfo=UTC)
# SYNTHETIC: a name needing JSON escapes, a non-ASCII broker (NFC), an archive
NAME = 'SYNTHETIC "A" \\ x\'); DROP TABLE app.account; --\né'
ACCOUNTS = (("SYN-A", "Société Générale", "TFSA - Self-directed",
             "CAD", NAME), ("SYN-B", "Interactive Brokers", "margin", "USD",
                            "SYNTHETIC B"),
            ("SYN-OLD", "SYN", "cash", "USD", "SYNTHETIC old"))  # fmt: skip
MAPS = (("SYN-BRK", "1001", (MappedInterval("SYN-A", V0),)),
        ("SYN-YAHOO", "pf-1", (MappedInterval("SYN-A", V0),)),
        ("SYN-BRK", "1002", (MappedInterval("SYN-OLD", V0),)),
        ("SYN-BRK", "1002", (MappedInterval("SYN-OLD", V0, V1),
                             MappedInterval("SYN-B", V1))))  # fmt: skip


def _key(keys: tuple[str, ...], r: dict[str, Any]) -> list[Any]:
    return [r[k] for k in keys]


def accounts_body(tenant: str = TENANT) -> dict[str, Any]:
    """`financial/accounts.json` as `account_store` stores and seals it: three
    accounts (SYN-OLD archived last), a broker source and a mirror, four mapping
    revisions (1002 has two), one knowledge time each in that order (SYNTHETIC)."""
    at = (datetime(2026, 1, 1, 0, 0, n, 250 * (n % 2), tzinfo=UTC) for n in range(10))
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in aw.ORDER}

    def row(table: str, content: str, **cols: Any) -> str:
        t = next(at)
        out[table].append({"tenant_id": tenant, **cols, "recorded_at": format_instant(
            t), "content_hash": content, "row_hash": aw.seal(content, t)})  # fmt: skip
        return aw.seal(content, t)

    heads = {}
    for aid, broker, kind, ccy, name in ACCOUNTS:
        a = UnderlyingAccount(aid, tenant, broker, kind, ccy)
        heads[aid] = row("accounts", aw.account_content(
            tenant, a, name, "complete_declared"), account_id=aid, name=name,
            broker=broker, account_type=kind, reporting_currency=ccy,
            analysis_scope="complete_declared")  # fmt: skip
    for sid, kind, provider in (("SYN-BRK", "broker_export", "syn-broker"),
                                ("SYN-YAHOO", "mirror", "yahoo")):  # fmt: skip
        content = aw.source_content(tenant, Source(sid, tenant, SourceKind(kind)),
                                    provider)  # fmt: skip
        row("sources", content, source_id=sid, kind=kind, provider=provider)
    prev: dict[tuple[str, str], tuple[int, str | None]] = {}
    for sid, ref, ivs in MAPS:
        rev, before = prev.get((sid, ref), (0, None))
        content = aw.revision_content(tenant, sid, ref, rev + 1, ivs, before)
        prev[(sid, ref)] = (rev + 1, row(
            "mappings", content, source_id=sid, account_ref=ref, revision=rev + 1,
            intervals=aw.intervals_json(ivs), previous_hash=before))  # fmt: skip
    row("account_revisions", aw.lifecycle_content(tenant, "SYN-OLD", 2, "archived",
        heads["SYN-OLD"]), account_id="SYN-OLD", revision=2, lifecycle="archived",
        previous_hash=heads["SYN-OLD"])  # fmt: skip
    for name, keys in aw.ORDER.items():
        out[name].sort(key=partial(_key, keys))
    return out


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


def test_a_section_with_no_destination_store_is_refused() -> None:
    learned, _ = exported()  # the T052 learned-state files: no destination writer
    b = pt.build_export("export-synth-1", LS_TENANT, AT, {
        pt.POLICIES_PATH: policy_rows(LS_TENANT),
        **{p: json.loads(raw) for p, raw in learned.files.items()}})  # fmt: skip
    rep = pt.verify_import(pt.canonical(b.manifest.to_wire()), b.files)
    assert refused(rep, LS_TENANT) is R.SECTION_NOT_APPLICABLE  # nothing written


def test_the_watchlists_are_planned_as_verified() -> None:
    """Increment 9: the watchlists are applied (0064's import function), so the
    plan carries the verified section; with no section, an empty one."""
    rows = watchlist_rows(TENANT)
    got = pa.plan(report({pt.WATCHLISTS_PATH: rows}), TENANT, None).watchlists
    assert list(got) == rows and pa.plan(report({}), TENANT, None).watchlists == ()


def test_the_journal_is_planned_as_verified() -> None:
    """Increment 7: the journal is applied (decision 112's import function), so
    the plan carries the verified section; with no section, an empty one."""
    body = journal(journal_rows(TENANT), TENANT)
    got = pa.plan(report({pt.POLICIES_PATH: policies(), pt.JOURNAL_PATH: body}),
                  TENANT, None)  # fmt: skip
    assert got.journal == body and len(got.policies) == len(policies())
    assert pa.plan(report({}), TENANT, None).journal == journal([], TENANT)


def test_the_accounts_are_planned_as_verified() -> None:
    """Increment 8: the accounts section is applied (0062's import function), so
    the plan carries the verified section; with no section, an empty one."""
    body = accounts_body()
    assert aw.parse_section(TENANT, body).accounts["SYN-OLD"].lifecycle == "archived"
    got = pa.plan(report({pt.ACCOUNTS_PATH: body}), TENANT, None)
    assert got.accounts == body
    assert pa.plan(report({}), TENANT, None).accounts == EMPTY[pt.ACCOUNTS_PATH]


def test_the_tombstones_are_planned_as_verified() -> None:
    """Increment 10: the tombstone ledger is applied (0066's import function), so
    the plan carries the verified section; with no section, an empty ledger."""
    body = EMPTY[pt.TOMBSTONES_PATH]
    assert pt.TOMBSTONES_PATH in pa.APPLIED
    assert pa.plan(report({pt.TOMBSTONES_PATH: body}), TENANT, None).tombstones == body
    assert pa.plan(report({}), TENANT, None).tombstones == body


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
