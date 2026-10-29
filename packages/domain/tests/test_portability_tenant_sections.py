"""The journal and policy export sections (T057 increment 2): closed, exact shapes,
re-verified rows, refusals on export and on import. Every tenant, account, source,
instrument, amount and answer is SYNTHETIC. Expected codes come from the
`qw_domain.portability` and `qw_domain.ledger_wire` docstring rules; the oracle for
each row is the domain itself (the journal and the policy history)."""

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
from qw_domain.journal import Journal
from qw_domain.onboarding import load_catalogue
from qw_domain.postings import Header, buy, deposit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import project_contract  # noqa: E402  # type: ignore[import-not-found]

R = pt.Refusal
TENANT = "tenant-synth-a"
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 1, 2, 15, tzinfo=UTC)
SYN = InstrumentId(uuid.UUID("00000000-0000-4000-8000-00000000005a"))
J, P = pt.JOURNAL_PATH, pt.POLICIES_PATH
CATALOGUE = load_catalogue(
    json.loads((ROOT / "docs/spec/config/onboarding_questions.json").read_text())
)
CONTRACT = {  # other records' required keys, which must never pass as journal rows
    name: project_contract.schema(name)["required"]
    for name in ("simulated_fill", "execution_report", "observation")
}


def journal_rows(tenant: str = TENANT) -> list[dict[str, Any]]:
    """Deposit, buy, and a reversal of the buy (SYNTHETIC), as the store exports."""
    h = [Header("SYN-ACCT", T0 + timedelta(days=d), SourceRef("SYN-BROKER", f"r{d}"))
         for d in range(3)]  # fmt: skip
    j = Journal()
    bought = buy(
        h[1], SYN, PositiveQuantity("2"), Price("100.25"), Money.of("1", "USD")
    )
    for n, e in enumerate((deposit(h[0], Money.of("1000", "USD")), bought,
                           bought.reversal(h[2].source))):  # fmt: skip
        j.post(e, T0 + timedelta(days=n, hours=1))
    gone = {e.event.reverses for e in j.entries() if e.event.reverses}
    return [lw.item(tenant, e, gone)[0] for e in j.entries()]


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
    sections: dict[str, object] = {J: journal_rows(), P: policy_rows()}
    b = build(sections)
    rep = pt.verify_import(pt.canonical(b.manifest.to_wire()), b.files)
    assert rep.data == sections and rep.unsupported == ()
    assert rep.omissions == pt.ALWAYS_OMITTED  # approvals are never carried
    ledger = Draft202012Validator(project_contract.schema("ledger_event"))
    for row in journal_rows():
        ledger.validate(row)
    statuses = [(r["kind"], r["status"]) for r in journal_rows()]
    assert statuses == [("deposit", "accepted"), ("buy", "reversed"),
                        ("correction", "accepted")]  # fmt: skip
    assert sorted(set(pt.SECTIONS) - pt.OPEN_SECTIONS) == [
        "audit/tombstones.json",
        "financial/accounts.json",
        "research/watchlists.json",
    ]


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
    rows = journal_rows()
    _edit(rows, n, path, value)
    assert refused(build, {J: rows}) is code
    raw = json.dumps(rows).encode()
    assert on_import(J, raw) is code


@pytest.mark.parametrize("record", sorted(CONTRACT))
def test_no_other_record_passes_as_a_journal_event(record: str) -> None:
    """Observations, claimed executions and simulated (virtual) fills stay
    separate: their fields are not journal fields."""
    row = dict.fromkeys(CONTRACT[record], "x")
    assert refused(build, {J: [row]}) is R.UNKNOWN_FIELD
    mixed = [*journal_rows(), {**journal_rows()[0], **row}]
    assert refused(build, {J: mixed}) is R.UNKNOWN_FIELD


def test_journal_order_and_repeats_are_refused() -> None:
    rows = journal_rows()
    assert refused(build, {J: [rows[0], rows[0]]}) is R.MALFORMED  # not a new event
    assert refused(build, {J: [rows[0], rows[2], rows[1]]}) is R.MALFORMED  # link
    assert refused(build, {J: rows[:2]}) is R.MALFORMED  # reversed with no reversal
    assert refused(build, {J: {"rows": rows}}) is R.MALFORMED


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
