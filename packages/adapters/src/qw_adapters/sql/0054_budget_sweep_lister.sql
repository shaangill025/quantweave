-- 0054: owner decision 100 (T037 increment 5). The worker's budget-sweep timer
-- must learn which tenants have an overdue reservation, but qw_worker reads
-- app.budget_book only inside one tenant's context (0052's tenant_isolation).
-- One fixed-SQL SECURITY DEFINER lister returns just tenant ids: those whose
-- book (any month) holds a `reserved` reservation with a deadline at or before
-- the database clock (clock_timestamp(), the clock the sweep uses; the guard
-- keeps every stored deadline in `format_instant` form). No book, period,
-- amount, reservation or count leaves it. Only qw_worker may execute it.
-- Under 0052's forced RLS the definer (owner qw_migrate) reads through
-- `overdue_list`, which admits only rows still holding a `reserved`
-- reservation and only while the session's login is a qw_worker member:
-- inside this function current_user is qw_migrate and session_user is the
-- worker login. A qw_migrate session whose login is not a qw_worker member (the
-- migration path) gains nothing; no other role's access changes (0053's
-- `operator_read` and every grant stay as they were). The other qw_migrate
-- definers qw_worker may execute (job_pick, outbox_pick, the purges, ...) are
-- fixed SQL that never reads app.budget_book.
-- Order and bound: ascending tenant id, at most 500 ids per call, keyset-paged
-- by `p_after` (NULL: from the start; else ids strictly greater). The page
-- bounds one statement's result and time; the caller (budget_store.
-- overdue_tenants) pages until a short page, so no tenant is starved by the
-- bound, and a tenant whose sweep keeps failing never hides the ones after it.

CREATE POLICY overdue_list ON app.budget_book FOR SELECT TO qw_migrate
    USING (pg_catalog.pg_has_role(session_user, 'qw_worker', 'MEMBER')
           AND book -> 'reservations' @> '[{"status": "reserved"}]'::jsonb);

CREATE FUNCTION app.budget_overdue_tenants(p_after uuid)
RETURNS SETOF uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT DISTINCT b.tenant_id
    FROM app.budget_book b
    WHERE (p_after IS NULL OR b.tenant_id > p_after)
      AND b.book -> 'reservations' @> '[{"status": "reserved"}]'::jsonb
      AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(b.book -> 'reservations') r
          WHERE r ->> 'status' = 'reserved'
            AND app.budget_book_instant(r -> 'deadline') <= clock_timestamp())
    ORDER BY b.tenant_id
    LIMIT 500
$$;

REVOKE ALL ON FUNCTION app.budget_overdue_tenants(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.budget_overdue_tenants(uuid) TO qw_worker;
