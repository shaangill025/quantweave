-- 0053: owner decision 98 (T050 increment 9). The `optimizer.cycle` job (qw_worker)
-- reserves the optimizer verdict's frozen cost in the operator's (bootstrap)
-- tenant book (0052); the assessor refuses to accept unless that reservation
-- exists with the frozen terms, so it needs to read that one tenant's book.
-- Narrowest form: no table privilege for qw_assessor. One fixed-SQL SECURITY
-- DEFINER reader returns, in one statement, the bootstrap tenant's id (0004
-- app.installation_bootstrap; already readable by qw_assessor, 0048) and that
-- tenant's latest book holding one reservation id (NULL columns if none), and
-- only qw_assessor may execute it. The caller cannot name a tenant or a period,
-- cannot see budget_authorization, and writes nothing. Under 0052's forced RLS
-- the definer (owner qw_migrate) reads through `operator_read`, which admits only
-- the bootstrap tenant's rows and only while the session's login is a qw_assessor
-- member: inside this function current_user is qw_migrate and session_user is the
-- assessor login. A qw_migrate session whose login is not a qw_assessor member
-- (the migration path) gains nothing. Hosted editions have no bootstrap row, so
-- the reader returns no row and the verdict stays BLOCKED (`budget_not_reserved`).

CREATE POLICY operator_read ON app.budget_book FOR SELECT TO qw_migrate
    USING (pg_catalog.pg_has_role(session_user, 'qw_assessor', 'MEMBER')
           AND tenant_id = (SELECT i.tenant_id FROM app.installation_bootstrap i));

CREATE FUNCTION app.budget_book_operator(p_reservation_id text)
RETURNS TABLE (tenant_id uuid, scope_id text, period text, version bigint,
               book jsonb, book_hash text, updated_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT i.tenant_id, b.scope_id, b.period, b.version, b.book, b.book_hash,
           b.updated_at
    FROM app.installation_bootstrap i
    LEFT JOIN LATERAL (
        SELECT x.scope_id, x.period, x.version, x.book, x.book_hash, x.updated_at
        FROM app.budget_book x
        WHERE x.tenant_id = i.tenant_id AND x.scope_id = x.tenant_id::text
          AND x.book -> 'reservations' @> jsonb_build_array(
              jsonb_build_object('reservation_id', p_reservation_id))
        ORDER BY x.period DESC
        LIMIT 1
    ) b ON true
$$;

REVOKE ALL ON FUNCTION app.budget_book_operator(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.budget_book_operator(text) TO qw_assessor;
