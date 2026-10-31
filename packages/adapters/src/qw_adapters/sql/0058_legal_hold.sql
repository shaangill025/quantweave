-- Durable legal holds (T058; spec §12; owner decisions 9, 22, 31).
-- Adapter: qw_adapters.legal_hold_store, which replays every row through the domain
-- `Holds` on load; a row the domain refuses makes the hold state unknown and every
-- deletion fails closed. One append-only chain per tenant (forced RLS): `place` or
-- `release` (two approvals as parallel arrays); one place and at most one release
-- per hold id, a release only after its place. Ids and codes only, no free text.
-- recorded_at (the event's time) is stamped here under the 'legal_hold:<tenant>'
-- lock (never before the latest event) and sealed: row_hash = sha256(entry_hash |
-- recorded_at); seq and prev_hash chain the entries. qw_app (the hold route) reads
-- and inserts (not recorded_at or row_hash); qw_worker reads. No update or delete.

CREATE TABLE app.legal_hold_event (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    seq bigint NOT NULL CHECK (seq BETWEEN 0 AND 2147483646),
    kind text NOT NULL CHECK (kind IN ('place', 'release')),
    hold_id text NOT NULL CHECK (app.budget_book_id(to_jsonb(hold_id))),
    reason text NOT NULL CHECK (reason ~ '^[a-z][a-z0-9_]{0,63}$'),
    authority_id text NOT NULL CHECK (app.budget_book_id(to_jsonb(authority_id))),
    classes text[] NOT NULL CHECK (classes <@ ARRAY['security', 'reference',
        'financial', 'policy', 'audit', 'research', 'market', 'evidence',
        'release', 'improvement', 'simulation', 'operations', 'privacy',
        'provider_payload', 'log', 'backup']),
    record_ids text[] NOT NULL CHECK (cardinality(record_ids) <= 1000
        AND app.deletion_tombstone_backups(record_ids)),  -- distinct sorted ids
    placed_by text CHECK (placed_by IN ('tenant_owner', 'operator')),
    principal_id text CHECK (principal_id IS NULL
        OR app.budget_book_id(to_jsonb(principal_id))),
    approval_roles text[] NOT NULL,
    approval_principals text[] NOT NULL,
    approval_receipts text[] NOT NULL,
    approval_step_ups timestamptz[] NOT NULL,
    solo boolean NOT NULL DEFAULT false,
    prev_hash text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    entry_hash text NOT NULL CHECK (entry_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, hold_id, kind),
    UNIQUE (tenant_id, entry_hash),
    CHECK (CASE kind
        WHEN 'place' THEN cardinality(classes) > 0 AND placed_by IS NOT NULL
            AND principal_id IS NOT NULL AND NOT solo
            AND cardinality(approval_roles) + cardinality(approval_principals)
                + cardinality(approval_receipts)
                + cardinality(approval_step_ups) = 0
        ELSE cardinality(classes) = 0 AND cardinality(record_ids) = 0
            AND placed_by IS NULL AND principal_id IS NULL
            AND array_ndims(approval_roles) = 1
            AND cardinality(approval_roles) = 2
            AND cardinality(approval_principals) = 2
            AND cardinality(approval_receipts) = 2
            AND cardinality(approval_step_ups) = 2
            AND approval_roles <@ ARRAY['tenant_owner', 'operator']
            AND app.budget_book_id(to_jsonb(approval_principals[1]))
            AND app.budget_book_id(to_jsonb(approval_principals[2]))
            AND app.budget_book_id(to_jsonb(approval_receipts[1]))
            AND app.budget_book_id(to_jsonb(approval_receipts[2]))
        END)
);

CREATE FUNCTION app.legal_hold_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    now_ CONSTANT timestamptz := clock_timestamp();
    p record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'legal_hold:' || NEW.tenant_id::text, 0));
    SELECT seq, entry_hash, recorded_at INTO p FROM app.legal_hold_event
        WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    NEW.recorded_at := greatest(now_, p.recorded_at);  -- any value given is replaced
    IF NEW.seq IS DISTINCT FROM coalesce(p.seq + 1, 0)
       OR NEW.prev_hash IS DISTINCT FROM coalesce(p.entry_hash, repeat('0', 64)) THEN
        RAISE EXCEPTION 'app.legal_hold_event: not the next entry'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.kind = 'release' AND NOT EXISTS (SELECT 1 FROM app.legal_hold_event h
            WHERE h.tenant_id = NEW.tenant_id AND h.hold_id = NEW.hold_id
            AND h.kind = 'place') THEN
        RAISE EXCEPTION 'app.legal_hold_event: not_found'
            USING ERRCODE = 'check_violation';
    END IF;
    NEW.row_hash := encode(sha256(convert_to(NEW.entry_hash || '|'
        || app.deletion_tombstone_instant(NEW.recorded_at), 'UTF8')), 'hex');
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.legal_hold_guard() FROM PUBLIC;
CREATE TRIGGER insert_guard BEFORE INSERT ON app.legal_hold_event
    FOR EACH ROW EXECUTE FUNCTION app.legal_hold_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.legal_hold_event
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.legal_hold_event ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.legal_hold_event
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.legal_hold_event TO qw_app, qw_worker;
GRANT INSERT (tenant_id, seq, kind, hold_id, reason, authority_id, classes,
    record_ids, placed_by, principal_id, approval_roles, approval_principals,
    approval_receipts, approval_step_ups, solo, prev_hash, entry_hash)
    ON app.legal_hold_event TO qw_app;
