"""Seed the six strategy families (T027; owner decision 10, 2026-10-15: "free or
delayed data feeds, the ai_thesis family pointed at the model gateway with a zero
budget so it can't spend, and every family marked not_tested and not
action-eligible"). Run by `qw-seed-strategies` as the schema owner at install and
at each release; nothing here is a migration.

- Manifests: the families' own reference manifests, built at run time, so each
  `implementation_hash` is the hash of the module source being installed (a SQL
  constant would go stale on the next code edit). `record_manifest` makes a rerun
  a no-op and refuses a changed manifest under a stored version (`immutable`):
  any edit to a family module needs its VERSION bumped before the release reseeds.
- Feeds: neutral ids naming a data class, never a vendor. Each is declared in the
  rights registry under the placeholder provider as `registered` (never connected
  or qualified) with every right unknown (`FAIL_CLOSED`), and its required uses are
  the uses the seeded manifests declare. No entitlement is recorded (decision 6:
  operator only), so `check_use` denies every use: the seeded families cannot act
  or research on these feeds until an operator records rights. A feed id that
  already exists is left exactly as stored.
- ai_thesis: `researcher.ai_thesis_manifest` (placeholder model ref, monthly budget
  bounded to 0) and the empty gateway chain `ROUTES` (`no_route`).
- One transaction: all or nothing. Lock order: strategy, then rights (rights is
  last, as in rights_store). A concurrent seed may be refused; rerun it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from qw_domain.allocation import allocation_manifest
from qw_domain.gateway import Route
from qw_domain.income import income_manifest
from qw_domain.opening_range import orb_manifest
from qw_domain.quality_value import quality_manifest
from qw_domain.researcher import ai_thesis_manifest
from qw_domain.rights import FAIL_CLOSED, FeedHistory, Provider, Use
from qw_domain.strategy_registry import StrategyManifest
from qw_domain.swing_momentum import trend_manifest

from qw_adapters import rights_store, strategy_store
from qw_adapters.tenancy import Conn

PROVIDER = Provider(
    "unassigned-free-source",
    "Placeholder: no provider chosen; free, delayed or end-of-day class only",
)
EOD, DELAYED, FILINGS = (
    "free-eod-daily-bars",  # end-of-day daily bars: allocation marks, trend
    "free-delayed-intraday-bars",  # delayed intraday bars: ORB research only
    "free-public-filings",  # public filings facts: quality, income, ai_thesis
)
FEEDS = (EOD, DELAYED, FILINGS)
ROUTES: tuple[Route, ...] = ()  # ai_thesis gateway chain: no route is configured


@dataclass(frozen=True, slots=True)
class Seeded:
    manifests: tuple[str, ...]  # "<strategy id>@<version>" newly recorded
    feeds: tuple[str, ...]  # feed ids newly declared


def manifests() -> tuple[StrategyManifest, ...]:
    return (
        allocation_manifest(EOD), quality_manifest(FILINGS), income_manifest(FILINGS),
        trend_manifest(EOD), orb_manifest(DELAYED), ai_thesis_manifest(FILINGS),
    )  # fmt: skip


def feed_uses(ms: Iterable[StrategyManifest]) -> dict[str, frozenset[Use]]:
    out: dict[str, frozenset[Use]] = {}
    for d in (d for m in ms for d in m.data):
        out[d.feed_id] = out.get(d.feed_id, frozenset()) | d.uses
    return out


@contextmanager
def connect(conninfo: str) -> Iterator[Conn]:
    """An autocommit connection acting as qw_migrate (the login must be a member)."""
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            conn.execute("SET ROLE qw_migrate")
            yield conn
    except psycopg.Error as exc:
        raise strategy_store.StrategyStoreError(
            "database", type(exc).__name__
        ) from None


def seed(conn: Conn, ms: Sequence[StrategyManifest] | None = None) -> Seeded:
    """Record `ms` (default: `manifests()`) and declare their missing feeds.
    `conn` acts as qw_migrate; a refusal raises the store's error and records
    nothing."""
    ms = manifests() if ms is None else tuple(ms)
    try:
        stored, missing = _seed(conn, ms)
    except psycopg.Error as exc:
        raise strategy_store.StrategyStoreError(
            "database", type(exc).__name__
        ) from None
    added = dict.fromkeys(f"{m.strategy_id}@{m.version}" for m in ms)
    return Seeded(tuple(k for k in added if k not in stored), tuple(sorted(missing)))


def _seed(conn: Conn, ms: tuple[StrategyManifest, ...]) -> tuple[set[str], list[str]]:
    with conn.transaction():
        rows = conn.execute("SELECT strategy_id, version FROM app.strategy_manifest")
        stored = {f"{s}@{v}" for s, v in rows.fetchall()}
        for m in ms:
            strategy_store.record_manifest(conn, m)
        held = {r[0] for r in conn.execute("SELECT id FROM app.rights_feed")}
        missing = {f: u for f, u in feed_uses(ms).items() if f not in held}
        if missing:
            rights_store.record_provider(conn, PROVIDER)
        (now,) = conn.execute("SELECT clock_timestamp()").fetchone() or (None,)
        for fid, uses in sorted(missing.items()):
            history = FeedHistory.new(fid, PROVIDER.provider_id)
            rights_store.record_feed(conn, history.revise(fid, uses, FAIL_CLOSED, now))
    return stored, list(missing)
