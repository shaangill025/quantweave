"""Budget reads (T037 increment 6): `GET /budgets` (listBudgets) over
qw_adapters.budget_store. No budget is created, changed or reserved here: there
is no mutating budget operation (the gateway, the sweep and the optimizer cycle
write the book).

- Any member reads the session tenant's books (0052: one per tenant and UTC
  month, scope the tenant id, forced RLS), as qw_app in the session tenant's
  transaction. Another tenant's book is never read. Every row is re-hashed and
  re-parsed on load; a row failing that is 409 `integrity`.
- Items are the spec's `BudgetSummary`, five per book, newest month first: the
  inference pool (`category` `inference`, the cap covering every category, spec
  §15), then each `budget_reservation` category in the config's priority order.
  Each category but improvement draws on the inference cap, so its `cap_usd`
  and `available_usd` are the pool's; improvement has its own cap inside it.
  This is `BudgetBook.summary(category)`. Amounts are decimal strings;
  `available_usd` is negative after a reported overrun.
- A month with no book has no items: nothing is shown as zero spend, and a
  tenant with no book gets an empty page.
- The C-02 page (project contract 0.2.0) carries the coverage the schema lacks,
  per item as `<kind>:<period>:<category>/<detail>`: a summary whose total
  exceeds `UsdBudget`'s bound is shown at that bound, named
  `saturated:<...>/totals_at_bound`; a book whose authorization ref is null,
  or is no longer recorded in `app.budget_authorization` (the owner revoked it;
  read in the same transaction, after the books), refuses every reservation
  whatever `available_usd` says, named `blocked:<...>/spending_not_authorized`
  or `blocked:<...>/spending_authorization_revoked`; monitoring, verification
  and research show the pool's cap and headroom, named
  `shared:<...>/cap_is_inference_pool`. Every reason makes the page `partial`,
  as on every served page (`qw_api.reads._page`), so a page with any of those
  three categories is `partial`.
- Keyset pages by (period descending, item position) with the qw_api.reads
  HMAC cursor."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from qw_adapters import budget_store as store
from qw_domain.budget import BudgetBook, Category

from qw_api.app import ApiError
from qw_api.policies import Reader
from qw_api.reads import Cursor, Limit, _after, _page

router = APIRouter(prefix="/api/v1")
SORT = "period_desc,pool"
INFERENCE = "inference"  # every category: the spec's monthly inference cap
POOLS: tuple[tuple[str, Category | None], ...] = (
    (INFERENCE, None), *((c.value, c) for c in Category)
)  # fmt: skip
Item = tuple[str, int, dict[str, Any], list[str]]


def blocked(book: BudgetBook, recorded: frozenset[str]) -> str | None:
    """Why the book refuses every new reservation, or None: no owner
    authorization ref, or one the owner has since revoked (0052 checks the ref
    against `app.budget_authorization` on every reservation and dispatch)."""
    ref = book.caps.authorization_ref
    if ref is None:
        return "spending_not_authorized"
    return None if ref in recorded else "spending_authorization_revoked"


def items(book: BudgetBook, recorded: frozenset[str]) -> list[Item]:
    """The book's summaries in page order, each with its coverage reasons."""
    out: list[Item] = []
    refused = blocked(book, recorded)
    for n, (name, category) in enumerate(POOLS):
        s, key = book.summary(category), f"{book.period}:{name}"
        why = []
        if category not in (None, Category.IMPROVEMENT):
            why.append(f"shared:{key}/cap_is_inference_pool")
        if s.saturated:
            why.append(f"saturated:{key}/totals_at_bound")
        if refused is not None:
            why.append(f"blocked:{key}/{refused}")
        view = {"scope_id": book.scope_id, "period": book.period, "category": name,
                "cap_usd": s.cap.to_wire(), "spent_usd": s.spent.to_wire(),
                "reserved_usd": s.reserved.to_wire(),
                "available_usd": s.available.to_wire()}  # fmt: skip
        out.append((book.period, n, view, why))
    return out


@router.get("/budgets")
def list_budgets(
    request: Request, principal: Reader, limit: Limit = 50, cursor: Cursor = None
) -> Response:
    after = _after(request, principal, SORT, cursor)
    through, _, last = (after or "").partition("|")
    # enough books for limit + 1 items after skipping up to one book's worth
    want = limit // len(POOLS) + 2
    with principal.tx() as tx:
        try:
            books = store.books(tx, through or None, want)
            recorded = store.recorded_refs(tx)  # read after the books
        except store.BudgetStoreError as exc:
            if exc.code != "integrity":
                raise  # database: a crash (500), never a client answer
            raise ApiError(409, "integrity", "Stored budgets failed checks.") from None
    rows = [r for b in books for r in items(b.book, recorded)
            if not (after and r[0] == through and r[1] <= int(last))]  # fmt: skip
    shown, more = rows[:limit], len(rows) > limit
    end = f"{shown[-1][0]}|{shown[-1][1]}" if more else None
    return _page(request, principal, SORT, [r[2] for r in shown], more,
                 [w for r in shown for w in r[3]], end)  # fmt: skip
