"""Pre-expiry scenario grids for a catalogue package (T035; R017; spec 09
"Pre-expiry analysis", "Payoff convention").

- A grid varies the underlying (relative shock), implied volatility (an absolute
  shift applied to every leg's volatility) and elapsed time. Before the expiry
  session's close every option leg is repriced with the T034 closed form
  (`option_greeks.bsm`, stdlib `decimal_math`), so each point inherits that model's
  assumptions and labels. At or after the close (the expiry slice) each leg is
  worth its exact intrinsic value, and the point's P&L is `OptionPackage.payoff`.
- Separate outputs per point (spec 09): the premium value of the option legs
  (signed, + long), the delta-equivalent exposure in underlying units (option
  deltas plus held shares; at expiry, N units per in-the-money contract), the
  whole-package P&L in the payoff convention (option value + stock value - stock
  basis + premiums in journal sign - fees), and the change in value against the
  unshocked base. The grid adds the cash collateral from the package's T034
  coverage check and the worst loss: max(0, -P&L) over the base and every point,
  so 0 when every point is profitable, and None when the grid has a gap (a gap
  may hide the worst point). An empty shock list is refused (`no_shocks`).
- The base point must be before the close and must price, or the grid is
  `Unavailable` (expired, missing volatility, stale or future spot, wrong session,
  unknown terms). A shocked spot keeps the
  base mark's age, so freshness is the same at every point. A point the model
  refuses (a non-positive shocked volatility) stays in the grid as a
  `ScenarioGap`; it is never zero-filled, and the grid is then incomplete.
- `model_applicable` is False if any repriced leg used the European approximation
  for an American contract. Discrete dividends and other discrete events are not
  modelled. Results are model estimates, not executable prices or probabilities.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import product

from qw_domain import decimal_math as dm
from qw_domain.decimals import Money, Price, Ratio
from qw_domain.identity import InstrumentId
from qw_domain.option_greeks import Greeks, ModelInputs, bsm
from qw_domain.option_packages import OptionPackage
from qw_domain.options import OptionRight
from qw_domain.valuation import Unavailable

_PRICE_STEP = Decimal(1).scaleb(-12)


@dataclass(frozen=True, slots=True)
class Shock:
    spot: Ratio  # relative: S' = S (1 + spot); must be > -1
    vol: Ratio  # absolute shift added to each leg's volatility
    elapsed: timedelta  # >= 0, added to the valuation time

    def __post_init__(self) -> None:
        if type(self.spot) is not Ratio or type(self.vol) is not Ratio:
            raise TypeError("shocks are Ratio values")
        if self.spot.value <= -1:
            raise ValueError("spot shock must be > -1")
        if type(self.elapsed) is not timedelta or self.elapsed < timedelta(0):
            raise ValueError("elapsed must be a non-negative timedelta")


def grid(
    spots: Sequence[Ratio], vols: Sequence[Ratio], elapsed: Sequence[timedelta]
) -> tuple[Shock, ...]:
    """The full product, spot-major."""
    return tuple(Shock(s, v, t) for s, v, t in product(spots, vols, elapsed))


@dataclass(frozen=True, slots=True)
class ScenarioPoint:
    shock: Shock
    spot: Price
    at_expiry: bool  # True: exact intrinsic values, no model
    premium_value: Decimal  # option legs, strike currency
    delta_units: Decimal  # underlying units, options plus shares
    pnl: Decimal  # whole package, payoff convention
    change: Decimal  # options plus shares, against the unshocked base


@dataclass(frozen=True, slots=True)
class ScenarioGap:
    shock: Shock
    reason: Unavailable


@dataclass(frozen=True, slots=True)
class ScenarioGrid:
    base: ScenarioPoint
    points: tuple[ScenarioPoint | ScenarioGap, ...]
    collateral: Money | None  # T034 cash required; None when coverage not checked
    worst_loss: Decimal | None  # max(0, -pnl) over base and points; None if a gap
    complete: bool
    model_applicable: bool
    currency: str


def _dec(f: Fraction) -> Decimal:
    return dm.div(Decimal(f.numerator), Decimal(f.denominator))


def _expiry(pkg: OptionPackage, s: Fraction) -> tuple[Decimal, Decimal]:
    """Exact intrinsic value and delta-equivalent units of the option legs."""
    value, units = Fraction(), Fraction()
    for leg, t in zip(pkg.legs, pkg.terms, strict=True):
        moneyness = t.units * s + t.cash - t.strike_cash
        put = leg.contract.right is OptionRight.PUT
        q = Fraction(leg.quantity.value)
        if (-moneyness if put else moneyness) > 0:
            value += q * (-moneyness if put else moneyness)
            units += q * (-t.units if put else t.units)
    return _dec(value), _dec(units)


def _point(
    pkg: OptionPackage,
    base: ModelInputs,
    vols: Mapping[InstrumentId, Ratio],
    shock: Shock,
) -> tuple[ScenarioPoint, bool] | Unavailable:
    c = dm.CTX
    spot = c.multiply(base.spot.price.value, c.add(1, shock.spot.value))
    price = Price(spot.quantize(_PRICE_STEP, context=c))
    at = base.valuation_at + shock.elapsed
    held = Decimal(0) if pkg.stock is None else pkg.stock.quantity.value
    applicable, expired = True, at >= base.expiry_session.close_at
    if expired:
        value, delta = _expiry(pkg, Fraction(price.value))
    else:
        value = delta = Decimal(0)
        moved = base.spot.observed_at + shock.elapsed
        mark = replace(base.spot, price=price, observed_at=moved)
        for leg in pkg.legs:
            vol = vols.get(leg.contract.contract_id)
            if vol is None:
                return Unavailable("vol_missing", "no volatility for an option leg")
            shifted = Ratio(c.add(vol.value, shock.vol.value))
            inputs = replace(base, spot=mark, vol=shifted, valuation_at=at)
            g = bsm(leg.contract, inputs)
            if not isinstance(g, Greeks):
                return g
            q = leg.quantity.value
            value = c.add(value, c.multiply(q, g.price))
            delta = c.add(delta, c.multiply(q, g.delta))
            applicable = applicable and g.applicable_to_style
    entry = sum((leg.premium.amount.value for leg in pkg.legs), Decimal(0))
    entry = c.subtract(entry, pkg.fees.amount.value)
    if pkg.stock is not None:
        entry = c.subtract(entry, pkg.stock.basis.amount.value)
    worth = c.add(value, c.multiply(held, price.value))
    point = ScenarioPoint(
        shock=shock,
        spot=price,
        at_expiry=expired,
        premium_value=value,
        delta_units=c.add(delta, held),
        pnl=c.add(worth, entry),
        change=worth,  # made relative to the base by `scenario_grid`
    )
    return point, applicable


def scenario_grid(
    pkg: OptionPackage,
    base: ModelInputs,
    vols: Mapping[InstrumentId, Ratio],
    shocks: Sequence[Shock],
) -> ScenarioGrid | Unavailable:
    """Reprice `pkg` at every shock; `base.vol` is replaced by `vols` per leg."""
    if base.valuation_at >= base.expiry_session.close_at:
        return Unavailable("contract_expired", "the base is at or after expiry")
    if not shocks:
        return Unavailable("no_shocks", "a grid needs at least one shock")
    zero = Ratio(0)
    start = _point(pkg, base, vols, Shock(zero, zero, timedelta(0)))
    if isinstance(start, Unavailable):
        return start
    first, applicable = start
    origin = first.change
    points: list[ScenarioPoint | ScenarioGap] = []
    for shock in shocks:
        r = _point(pkg, base, vols, shock)
        if isinstance(r, Unavailable):
            points.append(ScenarioGap(shock, r))
            continue
        p, ok = r
        points.append(replace(p, change=dm.CTX.subtract(p.change, origin)))
        applicable = applicable and ok
    complete = all(isinstance(p, ScenarioPoint) for p in points)
    losses = [-p.pnl for p in (first, *points) if isinstance(p, ScenarioPoint)]
    return ScenarioGrid(
        base=replace(first, change=Decimal(0)),
        points=tuple(points),
        collateral=None if pkg.cover is None else pkg.cover.cash_required,
        worst_loss=max(Decimal(0), *losses) if complete else None,
        complete=complete,
        model_applicable=applicable,
        currency=pkg.currency,
    )
