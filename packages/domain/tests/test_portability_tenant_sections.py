"""The journal, policy and watchlist export sections (T057 increments 2 and 3):
closed, exact shapes, re-verified rows, the journal chain header, refusals on export
and on import. Every tenant, account, source,
instrument, amount and answer is SYNTHETIC. Expected codes come from the
`qw_domain.portability` and `qw_domain.ledger_wire` docstring rules; the oracle for
each row is the domain itself (the journal and the policy history)."""

import hashlib
import json
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from qw_domain import ledger_wire as lw
from qw_domain import policy as dp
from qw_domain import portability as pt
from qw_domain.corporate_actions import SourceRef
from qw_domain.decimals import Money, PositiveQuantity, Price
from qw_domain.identity import InstrumentId
from qw_domain.instants import format_instant, parse_instant
from qw_domain.journal import Journal
from qw_domain.onboarding import load_catalogue
from qw_domain.postings import Header, buy, deposit
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "docs/spec/contracts/schemas"
sys.path.insert(0, str(ROOT / "tools"))
import project_contract  # noqa: E402  # type: ignore[import-not-found]

R = pt.Refusal
TENANT = "tenant-synth-a"
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
J, P, W = pt.JOURNAL_PATH, pt.POLICIES_PATH, pt.WATCHLISTS_PATH
CATALOGUE = load_catalogue(
    json.loads((ROOT / "docs/spec/config/onboarding_questions.json").read_text())
)
CONTRACT = {  # other records' required keys, which must never pass as journal rows
    name: project_contract.schema(name)["required"]
    for name in ("simulated_fill", "execution_report", "observation")
}


def journal_rows(tenant: str = TENANT) -> list[dict[str, Any]]:
    """Deposit, buy, and a reversal of the buy; then two deposits into a second
    account with the same `recorded_at` (SYNTHETIC), as the store exports."""
    h = [Header("SYN-ACCT", T0 + timedelta(days=d), SourceRef("SYN-BROKER", f"r{d}"))
         for d in range(3)]  # fmt: skip
    j, j2 = Journal(), Journal()
    bought = buy(
        h[1], SYN, PositiveQuantity("2"), Price("100.25"), Money.of("1", "USD")
    )
    for n, e in enumerate((deposit(h[0], Money.of("1000", "USD")), bought,
                           bought.reversal(h[2].source))):  # fmt: skip
        j.post(e, T0 + timedelta(days=n, hours=1))
    for n, cash in enumerate(("50", "70")):
        src = SourceRef("SYN-BROKER", f"t{n}")
        j2.post(deposit(Header("SYN-ACCT-2", T0, src), Money.of(cash, "CAD")),
                T0 + timedelta(days=5))  # fmt: skip
    gone = {e.event.reverses for e in j.entries() if e.event.reverses}
    return [lw.item(tenant, e, gone)[0] for e in (*j.entries(), *j2.entries())]


def journal(rows: list[dict[str, Any]], tenant: str = TENANT) -> dict[str, Any]:
    return lw.section(tenant, rows)


COND = {"metric": "price_cross", "operator": "gt", "data_spec_id": "feed-synth-iex",
        "description": "SYNTHETIC breakout"}  # fmt: skip


def watchlist_rows(tenant: str = TENANT) -> list[dict[str, Any]]:
    """Two live lists (SYNTHETIC), as `export_watchlists` gives them."""

    def entry(n: int, review: str | None) -> dict[str, Any]:
        return {"instrument_id": f"00000000-0000-4000-8000-00000000005{n}",
                "thesis": f"SYNTHETIC thesis {n}", "tags": ["syn"],
                "review_at": review, "monitoring_priority": 10 * n,
                "conditions": [{**COND, "values": ["100"]}],  # a fresh copy
                "invalidation_text": "SYNTHETIC: below 80",
                "needs": "realtime_exchange_limited"}  # fmt: skip

    return [{"id": "00000000-0000-4000-8000-0000000000a1", "tenant_id": tenant,
             "revision": 3, "name": "Growth", "description": "SYNTHETIC list",
             "entries": [entry(1, "2026-11-08T12:00:00Z"), entry(2, None)]},
            {"id": "00000000-0000-4000-8000-0000000000a2", "tenant_id": tenant,
             "revision": 1, "name": "Empty", "description": "", "entries": []}
            ]  # fmt: skip


def draft(objective: str = "growth", tenant: str = TENANT) -> dp.DraftPolicy:
    limits = [{"metric": m, "value": "0.05", "unit": "ratio", "window": "rolling_30d",
               "denominator": "account_nav", "account_id": "acct-syn-1",
               "currency": "CAD"} for m in sorted(dp.REQUIRED_METRICS)]  # fmt: skip
    answers: dict[str, Any] = {
        "ONB01": "selected_accounts", "ONB03": "CAD", "ONB04": [objective],
        "ONB05": "gt_7y", "ONB06": "none_known", "ONB07": "financially_manageable",
        "ONB10": "regular_session", "ONB11": ["stocks"], "ONB12": ["long_term"],
        "ONB14": limits, "ONB16": "rules_only",
        "ONB13": [{"account_id": "acct-syn-1", "currency": "CAD", "amount": "5000"}],
    }  # fmt: skip
    return dp.build_draft(tenant, CATALOGUE.validate(answers), (),
                          as_of=date(2026, 10, 9), synthetic=True)  # fmt: skip


def policy_rows(tenant: str = TENANT) -> list[dict[str, Any]]:
    h = dp.PolicyHistory.new("pol-synth-1", tenant)
    for d in (draft(tenant=tenant), draft("income", tenant)):
        h = h.propose(d)
    return [{"policy_id": h.policy_id, "version": v.version,
             "content_hash": v.content_hash, "label": v.draft.label,
             "content": dp.draft_to_json(v.draft)} for v in h.versions]  # fmt: skip


def build(sections: dict[str, object]) -> pt.ExportBundle:
    return pt.build_export("export-synth-1", TENANT, AT, sections)


def refused(fn: object, *args: object) -> pt.Refusal:
    with pytest.raises(pt.PortabilityError) as e:
        fn(*args)  # type: ignore[operator]
    return e.value.code


def on_import(path: str, raw: bytes) -> pt.Refusal:
    """A writer that skips `build_export`: a consistent manifest over `raw`."""
    entry = pt.FileEntry(path, pt.sha256(raw), len(raw), pt.SECTIONS[path])
    m = pt.ExportManifest("export-synth-1", pt.FORMAT_VERSION, AT, TENANT, (entry,),
                          pt.ALWAYS_OMITTED, (), pt.COMPATIBILITY_VERSION)  # fmt: skip
    return refused(pt.verify_import, pt.canonical(m.to_wire()), {path: raw})


def test_round_trip_is_exact_and_follows_the_contract() -> None:
    sections: dict[str, object] = {J: journal(journal_rows()), P: policy_rows(),
                                   W: watchlist_rows()}  # fmt: skip
    b = build(sections)
    rep = pt.verify_import(pt.canonical(b.manifest.to_wire()), b.files)
    assert rep.data == sections and rep.unsupported == ()
    assert rep.omissions == pt.ALWAYS_OMITTED  # approvals are never carried
    ledger = Draft202012Validator(project_contract.schema("ledger_event"))
    for row in journal_rows():
        ledger.validate(row)
    statuses = [(r["kind"], r["status"]) for r in journal_rows()]
    assert statuses == [("deposit", "accepted"), ("buy", "reversed"),
                        ("correction", "accepted"), ("deposit", "accepted"),
                        ("deposit", "accepted")]  # fmt: skip
    chains = journal(journal_rows())["chains"]
    assert [(c["account_id"], c["revision"], c["event_count"]) for c in chains] == [
        ("SYN-ACCT", 4, 3), ("SYN-ACCT-2", 3, 2)]  # fmt: skip
    common = json.loads((SCHEMAS / "common.schema.json").read_text())
    watch = Draft202012Validator(project_contract.schema("watchlist"),
                                 registry=Registry().with_resource(
                                     "common.schema.json",
                                     Resource.from_contents(common)))  # fmt: skip
    for w in watchlist_rows():  # the contract Watchlist, plus the declared extras
        watch.validate({k: v for k, v in w.items()
                        if k not in ("tenant_id", "description")}
                       | {"entries": [{k: v for k, v in e.items() if k != "needs"}
                                      for e in w["entries"]]})  # fmt: skip
    assert sorted(set(pt.SECTIONS) - pt.OPEN_SECTIONS) == [
        "audit/tombstones.json",  # no tombstone store yet (T058)
        "financial/accounts.json",  # no account, source or mapping store (T013)
    ]
    for closed in ("audit/tombstones.json", "financial/accounts.json"):
        assert refused(build, {closed: []}) is R.SECTION_NOT_READY


def _edit(rows: list[dict[str, Any]], n: int, path: str, value: object) -> None:
    *parents, leaf = path.split(".")
    obj: Any = rows[n]
    for p in parents:
        obj = obj[int(p)] if p.isdigit() else obj[p]
    if value is KeyError:
        del obj[leaf]
    else:
        obj[leaf] = value


JOURNAL_CASES = [  # row, dotted path, new value (KeyError deletes), refusal
    (0, "virtual", True, R.UNKNOWN_FIELD),
    (0, "simulation_id", "sim-synth-1", R.UNKNOWN_FIELD),
    (1, "postings.0.instrument_id", str(SYN.uuid), R.UNKNOWN_FIELD),
    (1, "instrument_movements.0.price", "100", R.UNKNOWN_FIELD),
    (0, "postings.0.amount", 1000.5, R.FLOAT),
    (0, "effective_at", KeyError, R.FIELD_MISSING),
    (0, "recorded_at", KeyError, R.FIELD_MISSING),
    (1, "postings.1.commodity", KeyError, R.FIELD_MISSING),
    (0, "effective_at", None, R.MALFORMED),
    (0, "effective_at", "2026-01-02T16:00:00+01:00", R.MALFORMED),  # not Z
    (0, "effective_at", "2026-01-02T15:00:01Z", R.MALFORMED),  # id no longer agrees
    (2, "recorded_at", "2026-01-01T00:00:00Z", R.MALFORMED),  # knowledge time reset
    (0, "postings.0.amount", "1000.0", R.MALFORMED),  # not canonical
    (0, "postings.0.amount", "1001", R.MALFORMED),  # unbalanced
    (1, "status", "accepted", R.MALFORMED),  # its reversal is in the section
    (1, "idempotency_key", "record/SYN-BROKER/r9", R.MALFORMED),
    (1, "economic_annotations", ["lot_policy:economic_average_cost/1"], R.MALFORMED),
    (0, "tenant_id", "tenant-synth-b", R.SCOPE_MISMATCH),
]


@pytest.mark.parametrize(("n", "path", "value", "code"), JOURNAL_CASES)
def test_journal_rows_are_refused_on_export_and_import(
    n: int, path: str, value: object, code: pt.Refusal
) -> None:
    body = journal(journal_rows())  # the header of the unedited rows
    _edit(body["events"], n, path, value)
    assert refused(build, {J: body}) is code
    assert on_import(J, json.dumps(body).encode()) is code


def edited(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """An archive edit: other rows under the honest header."""
    return {"chains": journal(journal_rows())["chains"], "events": rows}


@pytest.mark.parametrize("record", sorted(CONTRACT))
def test_no_other_record_passes_as_a_journal_event(record: str) -> None:
    """Observations, claimed executions and simulated (virtual) fills stay
    separate: their fields are not journal fields."""
    row = dict.fromkeys(CONTRACT[record], "x")
    assert refused(build, {J: edited([row])}) is R.UNKNOWN_FIELD
    mixed = [*journal_rows(), {**journal_rows()[0], **row}]
    assert refused(build, {J: edited(mixed)}) is R.UNKNOWN_FIELD


def test_journal_order_and_repeats_are_refused() -> None:
    rows = journal_rows()
    for bad in ([rows[0], rows[0]],  # not a new event
                [rows[0], rows[2], rows[1]],  # broken link
                rows[:2]):  # reversed with no reversal  # fmt: skip
        assert refused(build, {J: journal(bad)}) is R.MALFORMED
    assert refused(build, {J: rows}) is R.MALFORMED  # increment 2's bare list
    assert refused(build, {J: {"rows": rows}}) is R.UNKNOWN_FIELD
    assert refused(build, {J: {"events": rows}}) is R.FIELD_MISSING


def _shift(row: dict[str, Any], **delta: int) -> None:
    at = parse_instant(row["recorded_at"]) + timedelta(**delta)
    row["recorded_at"] = format_instant(at)


def test_knowledge_time_order_and_tail_are_bound_by_the_chain() -> None:
    """Increment 2's undetected archive edits are now `journal_chain`: each one
    replays, so only the header catches it. It shows integrity against edits of
    the rows alone; a rewriter of rows and header is caught only by a signature."""
    rows = journal_rows()
    backdated = journal_rows()
    _shift(backdated[1], hours=-1)  # still after row 0: the order holds
    swapped = [*rows[:3], rows[4], rows[3]]  # same recorded_at in SYN-ACCT-2
    assert rows[3]["recorded_at"] == rows[4]["recorded_at"]
    for bad in (backdated, swapped, rows[:4], rows[:3]):  # tail; a whole account
        assert lw.parse_section(bad)  # the rows alone replay
        assert refused(build, {J: edited(bad)}) is R.JOURNAL_CHAIN
        assert on_import(J, pt.canonical(edited(bad))) is R.JOURNAL_CHAIN
    honest = journal(rows)

    def header(n: int, key: str, value: object) -> pt.Refusal:
        body = journal(journal_rows())
        _edit(body["chains"], n, key, value)
        return refused(build, {J: body})

    assert header(0, "revision", 3) is R.JOURNAL_CHAIN
    assert header(1, "event_count", 3) is R.JOURNAL_CHAIN
    assert header(0, "head", "0" * 64) is R.JOURNAL_CHAIN
    assert header(0, "signature", "x") is R.UNKNOWN_FIELD
    assert header(0, "head", KeyError) is R.FIELD_MISSING
    assert header(0, "revision", 4.0) is R.FLOAT
    wrong: list[list[dict[str, Any]]] = [
        honest["chains"][:1], honest["chains"][::-1], [],
        journal(rows, "tenant-synth-b")["chains"]]  # fmt: skip
    for chains in wrong:
        assert refused(build, {J: {"chains": chains, "events": rows}}) is (
            R.JOURNAL_CHAIN)  # fmt: skip
    one = journal_rows()[:1]  # True == 1 in Python; the canonical bytes differ
    body = journal(one)
    body["chains"][0]["event_count"] = True
    assert refused(build, {J: body}) is R.JOURNAL_CHAIN
    assert build({J: journal([])}).files[J] == b'{"chains":[],"events":[]}'


def test_the_chain_formula_is_pinned() -> None:
    """A hand-computed vector (independent of `lw.chains`), and the header is
    sorted by account even when the section lists accounts in another order."""

    def h(*parts: object) -> str:
        text = json.dumps(parts, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode()).hexdigest()

    rows = journal_rows()
    tag, want = "qw-journal-chain/1", h("qw-journal-chain/1", TENANT, "SYN-ACCT-2")
    for n, r in enumerate(rows[3:], start=1):
        want = h(tag, want, n, r["id"], r["recorded_at"])
    later_first = [*rows[3:], *rows[:3]]
    chains = journal(later_first)["chains"]
    assert [c["account_id"] for c in chains] == ["SYN-ACCT", "SYN-ACCT-2"]
    assert chains[1] == {"account_id": "SYN-ACCT-2", "revision": 3,
                         "event_count": 2, "head": want}  # fmt: skip
    assert build({J: journal(later_first)})  # accounts in any order verify


SECOND = "00000000-0000-4000-8000-000000000052"  # the second entry's instrument
WATCHLIST_CASES = [  # row, dotted path, new value (KeyError deletes), refusal
    (0, "principal_id", "user-synth-1", R.UNKNOWN_FIELD),
    (0, "idempotency_key", "k-1", R.UNKNOWN_FIELD),
    (0, "entries.0.price", "100", R.UNKNOWN_FIELD),
    (0, "entries.0.conditions.0.secret", "x", R.UNKNOWN_FIELD),
    (0, "entries.0.monitoring_priority", 10.5, R.FLOAT),
    (0, "description", KeyError, R.FIELD_MISSING),
    (0, "entries.0.needs", KeyError, R.FIELD_MISSING),
    (0, "tenant_id", "tenant-synth-b", R.SCOPE_MISMATCH),
    (0, "revision", 0, R.MALFORMED),
    (0, "revision", True, R.MALFORMED),
    (0, "name", "", R.MALFORMED),
    (0, "id", "not an id", R.MALFORMED),
    (0, "entries.0.instrument_id", "SYN", R.MALFORMED),
    (0, "entries.0.instrument_id", SECOND, R.MALFORMED),  # a repeated instrument
    (0, "entries.0.review_at", "2026-11-08T13:00:00+01:00", R.MALFORMED),
    (0, "entries.0.monitoring_priority", 101, R.MALFORMED),
    (0, "entries.0.needs", "instant", R.MALFORMED),
    (0, "entries.0.conditions.0.operator", "between", R.MALFORMED),
    (0, "entries.0.tags", "syn", R.MALFORMED),
]


@pytest.mark.parametrize(("n", "path", "value", "code"), WATCHLIST_CASES)
def test_watchlist_rows_are_refused_on_export_and_import(
    n: int, path: str, value: object, code: pt.Refusal
) -> None:
    rows = watchlist_rows()
    _edit(rows, n, path, value)
    assert refused(build, {W: rows}) is code
    assert on_import(W, json.dumps(rows).encode()) is code


def test_watchlist_order_and_repeats_are_refused() -> None:
    a, b = watchlist_rows()
    assert refused(build, {W: [b, a]}) is R.MALFORMED
    assert refused(build, {W: [a, a]}) is R.MALFORMED
    swapped = {**a, "entries": a["entries"][::-1]}
    assert refused(build, {W: [swapped, b]}) is R.MALFORMED
    assert refused(build, {W: {"lists": [a]}}) is R.MALFORMED


POLICY_CASES = [
    (0, "accepted_by", "user-synth-1", R.UNKNOWN_FIELD),
    (0, "step_up_receipt_id", "s.1", R.UNKNOWN_FIELD),
    (0, "content.receipt", {}, R.UNKNOWN_FIELD),
    (0, "content.limits.0.signature", "x", R.UNKNOWN_FIELD),
    (0, "content.limits.0.value", 0.05, R.FLOAT),
    (0, "content_hash", KeyError, R.FIELD_MISSING),
    (0, "content.limits.0.window", KeyError, R.FIELD_MISSING),
    (0, "content.limits.0.value", "0.06", R.MALFORMED),  # hash no longer agrees
    (0, "content.limits.0.value", "0.050", R.MALFORMED),  # not canonical
    (0, "content.synthetic", 1, R.MALFORMED),
    (0, "label", "LIVE", R.MALFORMED),
    (0, "version", 2, R.MALFORMED),
    (1, "version", True, R.MALFORMED),
    (1, "policy_id", "pol synth", R.MALFORMED),
    (0, "content.tenant_id", "tenant-synth-b", R.SCOPE_MISMATCH),
]


@pytest.mark.parametrize(("n", "path", "value", "code"), POLICY_CASES)
def test_policy_rows_are_refused_on_export_and_import(
    n: int, path: str, value: object, code: pt.Refusal
) -> None:
    rows = policy_rows()
    _edit(rows, n, path, value)
    assert refused(build, {P: rows}) is code
    assert on_import(P, json.dumps(rows).encode()) is code


def test_policy_versions_must_replay_in_order() -> None:
    a, b = policy_rows()
    assert refused(build, {P: [b, a]}) is R.MALFORMED
    assert refused(build, {P: [a, a]}) is R.MALFORMED  # an unchanged version
    assert refused(build, {P: [a, {**b, "content": a["content"],
                                   "content_hash": a["content_hash"]}]}) is (
        R.MALFORMED)  # fmt: skip
    other = policy_rows("tenant-synth-b")
    assert refused(build, {P: [*policy_rows(), *other]}) is R.SCOPE_MISMATCH
