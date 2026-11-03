-- 0061 (T058): the hourly retention sweep's tenant lister (owner decision 113's
-- backstop) and the quarantine of a corrupt legal-hold row (owner decision 115).
-- Must be applied after 0060 (numbers are contiguous; 0060 lands separately).
--
-- Lister. The worker's sweep timer must learn every tenant, but qw_worker reads
-- app.tenant only inside one tenant's context (0002's forced RLS). As 0054 does
-- for budget books: one fixed-SQL SECURITY DEFINER function returns tenant ids
-- only, ascending, at most 500 per call, keyset-paged by `p_after`; only
-- qw_worker may execute it. Every tenant is listed, suspended ones too: expiry is
-- by age, and an erasure obligation does not lapse with the tenant's status. The
-- definer (qw_migrate) reads through `sweep_list`, which admits rows only while
-- the session's login is a qw_worker member. No other qw_migrate definer that
-- qw_worker or qw_app may execute reads app.tenant, so nobody else gains a row.
--
-- Quarantine. A corrupt hold row makes the tenant's hold state unknown and
-- blocks every deletion and hold action (0058). After owner approval, a superuser
-- (the `qw-hold-quarantine` job, never a running service) records here which row
-- is quarantined (by seq and a digest of its stored bytes, so a later edit needs
-- a fresh approval), who approved it and why (ids and a code, no free text), and
-- the scope it blocks, stated by the approver because the corrupt row's own scope
-- cannot be trusted (default: every class of the tenant). In the same transaction
-- the job appends an ordinary `place` of `replacement_hold_id` with exactly that
-- scope, so the block lasts until a valid two-person release of the replacement.
-- `next_prev_hash` is the link the next row carries (replay crosses the corrupt
-- row by it). The guard refuses any session that is not a superuser acting as
-- itself (qw_migrate, the owner, included) and stamps who ran it and when.
-- Append-only; qw_app and qw_worker read under forced RLS.

CREATE POLICY sweep_list ON app.tenant FOR SELECT TO qw_migrate
    USING (pg_catalog.pg_has_role(session_user, 'qw_worker', 'MEMBER'));

CREATE FUNCTION app.retention_sweep_tenants(p_after uuid)
RETURNS SETOF uuid
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT t.id FROM app.tenant t
    WHERE p_after IS NULL OR t.id > p_after
    ORDER BY t.id
    LIMIT 500
$$;

REVOKE ALL ON FUNCTION app.retention_sweep_tenants(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.retention_sweep_tenants(uuid) TO qw_worker;

CREATE TABLE app.legal_hold_quarantine (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    seq bigint NOT NULL CHECK (seq BETWEEN 0 AND 2147483646),
    row_digest text NOT NULL CHECK (row_digest ~ '^[0-9a-f]{64}$'),
    next_prev_hash text NOT NULL CHECK (next_prev_hash ~ '^[0-9a-f]{64}$'),
    replacement_hold_id text NOT NULL
        CHECK (app.budget_book_id(to_jsonb(replacement_hold_id))),
    classes text[] NOT NULL CHECK (cardinality(classes) > 0 AND classes <@ ARRAY[
        'security', 'reference', 'financial', 'policy', 'audit', 'research',
        'market', 'evidence', 'release', 'improvement', 'simulation',
        'operations', 'privacy', 'provider_payload', 'log', 'backup']),
    record_ids text[] NOT NULL CHECK (cardinality(record_ids) <= 1000
        AND app.deletion_tombstone_backups(record_ids)),  -- distinct sorted ids
    approved_by text NOT NULL CHECK (app.budget_book_id(to_jsonb(approved_by))),
    approval_ref text NOT NULL CHECK (app.budget_book_id(to_jsonb(approval_ref))),
    reason text NOT NULL CHECK (reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    quarantined_by text NOT NULL DEFAULT '',
    quarantined_at timestamptz NOT NULL DEFAULT '-infinity',
    PRIMARY KEY (tenant_id, seq, row_digest),
    UNIQUE (tenant_id, replacement_hold_id)
);

CREATE FUNCTION app.legal_hold_quarantine_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF current_user IS DISTINCT FROM session_user OR NOT coalesce((SELECT
            r.rolsuper FROM pg_roles r WHERE r.rolname = session_user), false) THEN
        RAISE EXCEPTION 'app.legal_hold_quarantine: superuser only'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'legal_hold:' || NEW.tenant_id::text, 0));
    IF NOT EXISTS (SELECT 1 FROM app.legal_hold_event e
            WHERE e.tenant_id = NEW.tenant_id AND e.seq = NEW.seq) THEN
        RAISE EXCEPTION 'app.legal_hold_quarantine: not_found'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.quarantined_by := session_user::text;  -- any value given is replaced
    NEW.quarantined_at := clock_timestamp();
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.legal_hold_quarantine_guard() FROM PUBLIC;
CREATE TRIGGER insert_guard BEFORE INSERT ON app.legal_hold_quarantine
    FOR EACH ROW EXECUTE FUNCTION app.legal_hold_quarantine_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.legal_hold_quarantine
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.legal_hold_quarantine ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.legal_hold_quarantine
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.legal_hold_quarantine TO qw_app, qw_worker;
