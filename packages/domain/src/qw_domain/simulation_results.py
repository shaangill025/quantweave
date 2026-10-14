"""Ledger-only simulation results as calculation records (T041 increment 4, T042;
spec 5 numeric-function outputs, spec 14; R063, R064 in part).

`ledger_results` turns one simulation state (its virtual ledger and the digest of the
whole stored simulation) into T018 `CalculationRecord`s, through
`record_calculation`:
- Figures that need no market data: ending cash, fees, income, the units of every
  instrument the ledger has touched (0 once sold out), and realized gain two ways:
  - `virtual_realized_net` is the spec convention (spec 5 §15, spec 4 §17): each
    buy adds price x quantity + its fee to the instrument's cost pool; each sell
    allocates the pool in proportion to the units sold (rounded half-even to 1e-12,
    `NET_ROUNDING`; a full close takes the whole pool) and books its net proceeds
    (the ledger cash, which is gross less the sell's fee) less that allocation.
    It is rebuilt from the ledger entries; the simulator's own bookkeeping, which
    keeps fees out of cost, is unchanged. Seeds add their cost, splits only units,
    dividends nothing; any other entry kind (options) makes it
    `unsupported_entry_kind`.
  - `virtual_realized_gain_before_fees` is the simulator ledger's `realized`:
    proceeds before fees less average cost before fees.
  The other figures are exact sums of ledger amounts, which the simulator keeps at
  1e-12 (`LEDGER_ROUNDING`); coverage `complete` for the ledger.
- Figures that need prices or benchmark levels (market value, total return,
  benchmark comparison) are records with status `insufficient_inputs` and a named
  `Unavailable` code (`PRICED_CODES`); no price is assumed. A scorecard is not a
  single figure and is not produced here at all.
- Every record's inputs name the simulation and its digest (references, so they
  appear as `ref:` warnings in the contract projection), the ledger length and the
  starting capital; its warnings start with `simulated:separate_from_actual` and
  then the caller's markings. Records are content-addressed and exclude
  `computed_at`, so recomputing a state gives the same ids, and any other state
  (another digest) gives different ones.
`as_of` is the state's as-of and may not precede any ledger entry.
"""

import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal

from qw_domain.calculation import CalcInput, CalculationRecord, record_calculation
from qw_domain.decimals import DOMAIN_CONTEXT, Money, Quantity
from qw_domain.instants import ensure_aware_utc
from qw_domain.simulation import EntryKind, VirtualPortfolio, _share
from qw_domain.valuation import Unavailable

SIMULATED = "simulated:separate_from_actual"
LEDGER_ROUNDING = "exact:ledger_sum_1e-12"
NET_ROUNDING = "half_even:1e-12_per_allocation"
ENDING_CASH = "virtual_ending_cash/1"
REALIZED = "virtual_realized_gain_before_fees/1"
REALIZED_NET = "virtual_realized_net/1"
FEES = "virtual_fees/1"
INCOME = "virtual_income/1"
UNITS = "virtual_position_units/1"
PRICED_CODES = {
    "virtual_market_value": "missing_market_inputs",
    "virtual_total_return": "missing_market_inputs",
    "virtual_benchmark_comparison": "missing_benchmark_series",
}
_WHY = {
    "missing_market_inputs": "no price observations are held for this simulation",
    "missing_benchmark_series": "no benchmark series is held for this simulation",
}
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)


def realized_net(p: VirtualPortfolio) -> Money | Unavailable:
    """Realized net P&L with fees in lot cost and off proceeds (module docstring)."""
    pool: dict[object, Decimal] = {}
    held: dict[object, Decimal] = {}
    total = Decimal(0)
    with localcontext(DOMAIN_CONTEXT):
        for e in p.entries:
            k = e.instrument_id
            if e.kind in (EntryKind.CAPITAL, EntryKind.DIVIDEND):
                continue
            if e.kind is EntryKind.SPLIT:
                held[k] = held.get(k, Decimal(0)) + e.units
            elif e.kind is EntryKind.SEED or (e.kind is EntryKind.FILL
                                               and e.units > 0):  # fmt: skip
                pool[k] = pool.get(k, Decimal(0)) + e.cost + e.fees
                held[k] = held.get(k, Decimal(0)) + e.units
            elif e.kind is EntryKind.FILL and e.units < 0:
                q, have = -e.units, held.get(k, Decimal(0))
                if q > have:
                    return Unavailable("negative_units", f"sell {e.entry_id}")
                cut = _share(pool[k], q, have)  # the simulator's own allocation
                pool[k] -= cut
                held[k] = have - q
                total += e.cash - cut  # cash is the net proceeds
            else:
                return Unavailable("unsupported_entry_kind", e.kind.value)
    return Money.of(total, p.currency)


def ledger_results(
    simulation_id: str,
    digest: str,
    portfolio: VirtualPortfolio,
    *,
    as_of: datetime,
    computed_at: datetime,
    warnings: Sequence[str] = (),
) -> tuple[CalculationRecord, ...]:
    p, sid = portfolio, simulation_id
    if p.virtual is not True or p.account_id != sid or not p.entries:
        raise ValueError("not the virtual ledger of this simulation")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError("digest must be a lowercase sha256 hex")
    at = ensure_aware_utc(as_of)
    if at < max(e.effective_at for e in p.entries):
        raise ValueError("as_of is before the ledger's latest entry")
    cur = p.currency
    base = [
        CalcInput.reference("simulation", sid, "simulation", sid),
        CalcInput.reference("simulation_digest", digest, "sha256", sid),
        CalcInput.decimal("ledger_entries", Decimal(len(p.entries)), "entries", sid),
        CalcInput.money("starting_capital", Money.of(p.entries[0].capital, cur), sid),
    ]
    marks = (SIMULATED, *warnings)

    def rec(
        method: str, outcome: Money | Quantity | Unavailable, unit: str,
        extra: Sequence[CalcInput] = (),
    ) -> CalculationRecord:  # fmt: skip
        gap = isinstance(outcome, Unavailable)
        coverage: Literal["complete", "partial"] = "partial" if gap else "complete"
        rounding = NET_ROUNDING if method == REALIZED_NET else LEDGER_ROUNDING
        return record_calculation(
            method, [*base, *extra], outcome, unit=unit, rounding=rounding,
            as_of=at, computed_at=computed_at, warnings=marks, coverage=coverage,
        )  # fmt: skip

    out = [
        rec(ENDING_CASH, Money.of(p.cash(), cur), cur),
        rec(REALIZED_NET, realized_net(p), cur),
        rec(REALIZED, Money.of(p.realized(), cur), cur),
        rec(FEES, Money.of(p.fees(), cur), cur),
        rec(INCOME, Money.of(p.income(), cur), cur),
    ]
    for iid in p.instruments():
        ref = CalcInput.reference("instrument", iid.to_wire(), "instrument", sid)
        out.append(rec(UNITS, Quantity(p.units(iid)), "units", [ref]))
    for name, code in PRICED_CODES.items():
        gap = Unavailable(code, _WHY[code])
        out.append(rec(f"{name}/1", gap, cur))
    return tuple(out)
