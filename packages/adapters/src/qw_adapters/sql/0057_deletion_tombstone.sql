-- The durable T058 tombstone ledger (T058 increment 5; spec §4 "Corrections and
-- retention", §12 "Privacy and lifecycle", §15; data dictionary
-- `deletion_tombstone` (scope tenant_or_control; "minimal identifiers only");
-- qw_domain.retention TombstoneLedger and LedgerCheckpoint). Adapter:
-- qw_adapters.tombstone_store, which rebuilds every row as a domain Tombstone and
-- re-verifies the chain (`retention.verify`) and the checkpoints on every load.
-- - Scope: one chain per tenant (forced RLS), so a tenant's ledger can be verified
--   and exported inside its own transaction. Installation (control) tombstones
--   are not stored here.
-- - `deletion_tombstone`: the domain Tombstone, nothing else. Identifiers, hashes,
--   the class and times only: the record is named by its pseudonym `record_hash`
--   (owner decision 104: an HMAC under the installation's key, which never enters
--   the database; `hash_scheme` names the scheme), and no column can hold deleted
--   content (no JSON; ids are ID_PATTERN strings the caller chooses). seq =
--   previous + 1 from 0, prev_hash = the previous entry_hash (or 64 zeros), one
--   tombstone per (tenant, record_hash), all under one `key_id` (a one-way MAC
--   naming the key; another key is `key_mismatch`). The pseudonym binds the record's
--   incarnation, so only a retry finds an existing row. The guard recomputes
--   `entry_hash` exactly as qw_domain.retention.entry_hash does (compact JSON of
--   the stored fields; nothing in them needs escaping), so a writer cannot seal a
--   row the domain would refuse; the store re-verifies on load.
-- - `deletion_tombstone_checkpoint`: the domain LedgerCheckpoint (length, head)
--   after each append; the guard requires length = the tenant's tombstone count
--   and head = the entry_hash at seq length - 1. It lives in the same database,
--   so it detects a dropped tail but not a rollback of the whole database: the
--   anchor outside the backup failure domain is still the caller's.
-- - Time: recorded_at is stamped here under the 'deletion_tombstone:<tenant>'
--   lock (the database clock, never before the tenant's latest row in either
--   table), and row_hash = sha256(<entry_hash or head> | recorded_at in
--   format_instant form) seals it. deleted_at is inside the domain digest, so it
--   is checked instead: within the last 60 s of the database clock
--   (`stale_clock`), never before the previous tombstone or the job's
--   requested_at (`time_order`).
-- - qw_worker (the deletion job runner): SELECT and column INSERT (not recorded_at
--   or row_hash). qw_app: SELECT (export). Nobody updates or deletes.

CREATE FUNCTION app.deletion_tombstone_instant(t timestamptz) RETURNS text
LANGUAGE sql STABLE STRICT SET search_path = pg_catalog
AS $$  -- qw_domain.instants.format_instant
    SELECT to_char(t AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN date_trunc('second', t) = t THEN ''
                ELSE to_char(t AT TIME ZONE 'UTC', '.US') END || 'Z'
$$;
REVOKE ALL ON FUNCTION app.deletion_tombstone_instant(timestamptz) FROM PUBLIC;

CREATE FUNCTION app.deletion_tombstone_backups(a text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog
AS $$  -- distinct ids, sorted by code point (the domain sorts by backup_id)
    SELECT cardinality(a) <= 1000 AND array_ndims(a) IS NOT DISTINCT FROM
            CASE WHEN cardinality(a) = 0 THEN NULL ELSE 1 END
        AND coalesce((SELECT bool_and(app.budget_book_id(to_jsonb(b)))
                      FROM unnest(a) b), true)
        AND a = ARRAY(SELECT DISTINCT b COLLATE "C" FROM unnest(a) b ORDER BY 1)
$$;
REVOKE ALL ON FUNCTION app.deletion_tombstone_backups(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.deletion_tombstone_backups(text[]),
    app.deletion_tombstone_instant(timestamptz) TO qw_app, qw_worker;

CREATE TABLE app.deletion_tombstone (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    seq bigint NOT NULL CHECK (seq BETWEEN 0 AND 2147483646),
    record_hash text NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    cls text NOT NULL CHECK (cls IN ('security', 'reference', 'financial',
        'policy', 'audit', 'research', 'market', 'evidence', 'release',
        'improvement', 'simulation', 'operations', 'provider_payload', 'log',
        'backup')),  -- RetentionClass less `privacy`: the ledger is never deleted
    job_id text NOT NULL CHECK (app.budget_book_id(to_jsonb(job_id))),
    requested_at timestamptz NOT NULL,
    deleted_at timestamptz NOT NULL,
    residual_backups text[] NOT NULL
        CHECK (app.deletion_tombstone_backups(residual_backups)),
    backup_expiry_at timestamptz,
    hash_scheme text NOT NULL CHECK (hash_scheme IN ('qw-record-hmac-1')),
    key_id text NOT NULL CHECK (key_id ~ '^[0-9a-f]{32}$'),  -- one-way, not a key
    prev_hash text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    entry_hash text NOT NULL CHECK (entry_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, record_hash),
    UNIQUE (tenant_id, entry_hash),
    CHECK (requested_at <= deleted_at),
    CHECK (backup_expiry_at IS NULL OR cardinality(residual_backups) > 0)
);
CREATE INDEX deletion_tombstone_requested ON app.deletion_tombstone
    (tenant_id, requested_at);

CREATE TABLE app.deletion_tombstone_checkpoint (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    length bigint NOT NULL CHECK (length BETWEEN 1 AND 2147483647),
    head text NOT NULL CHECK (head ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, length),
    FOREIGN KEY (tenant_id, head)
        REFERENCES app.deletion_tombstone (tenant_id, entry_hash)
);

CREATE FUNCTION app.deletion_tombstone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    now_ CONSTANT timestamptz := clock_timestamp();
    latest timestamptz;
    p record;
    n bigint;
    sealed text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'deletion_tombstone:' || NEW.tenant_id::text, 0));
    SELECT max(t) INTO latest FROM (
        SELECT max(recorded_at) t FROM app.deletion_tombstone
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.deletion_tombstone_checkpoint
            WHERE tenant_id = NEW.tenant_id) x;
    NEW.recorded_at := greatest(now_, latest);  -- any value given is replaced
    SELECT seq, entry_hash, deleted_at, key_id INTO p FROM app.deletion_tombstone
        WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    IF TG_TABLE_NAME = 'deletion_tombstone' THEN
        IF NEW.seq IS DISTINCT FROM coalesce(p.seq + 1, 0)
           OR NEW.prev_hash IS DISTINCT FROM coalesce(p.entry_hash, repeat('0', 64))
        THEN
            RAISE EXCEPTION 'app.deletion_tombstone: not the next entry'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.key_id IS DISTINCT FROM coalesce(p.key_id, NEW.key_id) THEN
            RAISE EXCEPTION 'app.deletion_tombstone: key_mismatch'
                USING ERRCODE = 'check_violation';  -- one key per chain
        END IF;
        IF NEW.deleted_at < p.deleted_at THEN
            RAISE EXCEPTION 'app.deletion_tombstone: time_order'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.deleted_at > greatest(now_, p.deleted_at)
           OR NEW.deleted_at < now_ - interval '60 seconds' THEN
            RAISE EXCEPTION 'app.deletion_tombstone: stale_clock'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.entry_hash IS DISTINCT FROM encode(sha256(convert_to(
            '["qw-tombstone-2",' || NEW.seq || ',"' || NEW.tenant_id || '","'
            || NEW.record_hash || '","' || NEW.cls || '","' || NEW.job_id || '","'
            || app.deletion_tombstone_instant(NEW.requested_at) || '","'
            || app.deletion_tombstone_instant(NEW.deleted_at) || '",['
            || coalesce((SELECT string_agg('"' || bk || '"', ',' ORDER BY ord)
                FROM unnest(NEW.residual_backups) WITH ORDINALITY u (bk, ord)), '')
            || '],' || coalesce('"' || app.deletion_tombstone_instant(
                NEW.backup_expiry_at) || '"', 'null')
            || ',"' || NEW.hash_scheme || '","' || NEW.key_id || '","'
            || NEW.prev_hash || '"]',
            'UTF8')), 'hex') THEN
            RAISE EXCEPTION 'app.deletion_tombstone: entry_hash'
                USING ERRCODE = 'check_violation';
        END IF;
        sealed := NEW.entry_hash;
    ELSE
        n := coalesce(p.seq + 1, 0);
        IF NEW.length IS DISTINCT FROM n OR NEW.head IS DISTINCT FROM p.entry_hash
           OR EXISTS (SELECT 1 FROM app.deletion_tombstone_checkpoint c
                      WHERE c.tenant_id = NEW.tenant_id AND c.length >= n) THEN
            RAISE EXCEPTION 'app.deletion_tombstone_checkpoint: not the head'
                USING ERRCODE = 'check_violation';
        END IF;
        sealed := NEW.head;
    END IF;
    NEW.row_hash := encode(sha256(convert_to(sealed || '|'
        || app.deletion_tombstone_instant(NEW.recorded_at), 'UTF8')), 'hex');
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.deletion_tombstone_guard() FROM PUBLIC;
CREATE TRIGGER insert_guard BEFORE INSERT ON app.deletion_tombstone
    FOR EACH ROW EXECUTE FUNCTION app.deletion_tombstone_guard();
CREATE TRIGGER insert_guard BEFORE INSERT ON app.deletion_tombstone_checkpoint
    FOR EACH ROW EXECUTE FUNCTION app.deletion_tombstone_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.deletion_tombstone
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.deletion_tombstone_checkpoint
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.deletion_tombstone ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.deletion_tombstone
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.deletion_tombstone_checkpoint ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.deletion_tombstone_checkpoint
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.deletion_tombstone, app.deletion_tombstone_checkpoint
    TO qw_app, qw_worker;
GRANT INSERT (tenant_id, seq, record_hash, cls, job_id, requested_at, deleted_at,
    residual_backups, backup_expiry_at, hash_scheme, key_id, prev_hash, entry_hash)
    ON app.deletion_tombstone TO qw_worker;
GRANT INSERT (tenant_id, length, head) ON app.deletion_tombstone_checkpoint
    TO qw_worker;
