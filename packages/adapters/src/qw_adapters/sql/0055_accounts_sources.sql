-- Accounts, sources and source-mapping revisions (T013 increment 2; spec §4
-- "Canonical boundaries"; data dictionary account, source_connection and
-- account_source_binding; contract Account). Adapter: qw_adapters.account_store,
-- which re-hashes every row and replays it through qw_domain.sources.SourceMap.
-- - `account`: the underlying (custody) account, registered once, immutable here
--   (metadata revisions, archive and the derived status/cash_basis are later).
-- - `data_source`: a feed reporting on accounts. `kind` is qw_domain SourceKind;
--   'mirror' marks a Yahoo-style report-only view. `provider` is a label. No
--   credential, token, secret reference or connection state is stored anywhere.
-- - `source_mapping_revision`: append-only, per (source, account_ref) revision =
--   previous + 1 from 1, each naming the previous row hash; `intervals` is the
--   complete half-open [valid_from, valid_to) list, each naming an account of the
--   same tenant, sorted, non-overlapping.
-- - recorded_at is stamped here, never supplied (as 0007's ledger_event_stamp):
--   the database clock under the 'source_map:<tenant>' lock, and never before
--   any earlier row of the tenant in these three tables (one knowledge order, as
--   SourceMap needs). The caller supplies `content_hash` (the store's digest of
--   every other column); the guard seals it: row_hash = sha256(content_hash ||
--   '|' || recorded_at in format_instant form), so both stay functions of the row.
-- - qw_app: SELECT and column INSERT (not recorded_at or row_hash). qw_worker:
--   SELECT. Nobody updates or deletes.

CREATE TABLE app.account (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    account_id text NOT NULL CHECK (app.budget_book_id(to_jsonb(account_id))),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    broker text NOT NULL CHECK (broker ~ '^[!-~]{1,128}$'),
    account_type text NOT NULL CHECK (account_type ~ '^[!-~]{1,128}$'),
    reporting_currency text NOT NULL CHECK (reporting_currency ~ '^[A-Z]{3}$'),
    analysis_scope text NOT NULL CHECK (analysis_scope IN
        ('complete_declared', 'partial_declared', 'hypothetical')),
    recorded_at timestamptz NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, account_id)
);

CREATE TABLE app.data_source (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    source_id text NOT NULL CHECK (app.budget_book_id(to_jsonb(source_id))),
    kind text NOT NULL CHECK (kind IN
        ('broker_api', 'broker_export', 'aggregator', 'manual', 'mirror')),
    provider text NOT NULL CHECK (app.budget_book_id(to_jsonb(provider))),
    recorded_at timestamptz NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, source_id)
);

CREATE TABLE app.source_mapping_revision (
    tenant_id uuid NOT NULL,
    source_id text NOT NULL,
    account_ref text NOT NULL CHECK (account_ref ~ '^[!-~]{1,128}$'),
    revision integer NOT NULL CHECK (revision BETWEEN 1 AND 2147483646),
    intervals jsonb NOT NULL CHECK (jsonb_typeof(intervals) = 'array'
        AND jsonb_array_length(intervals) BETWEEN 1 AND 1000),
    recorded_at timestamptz NOT NULL,
    previous_hash text CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, source_id, account_ref, revision),
    CHECK ((revision = 1) = (previous_hash IS NULL)),
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.data_source (tenant_id, source_id)
);

CREATE FUNCTION app.source_map_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    latest timestamptz;
    p record;
    bad boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'source_map:' || NEW.tenant_id::text, 0));
    SELECT max(t) INTO latest FROM (
        SELECT max(recorded_at) t FROM app.account WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.data_source
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.source_mapping_revision
            WHERE tenant_id = NEW.tenant_id) x;
    NEW.recorded_at := greatest(clock_timestamp(), latest);  -- any value given
    NEW.row_hash := encode(sha256(convert_to(NEW.content_hash || '|'
        || to_char(NEW.recorded_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN date_trunc('second', NEW.recorded_at) = NEW.recorded_at
                THEN '' ELSE to_char(NEW.recorded_at AT TIME ZONE 'UTC', '.US') END
        || 'Z', 'UTF8')), 'hex');
    IF TG_TABLE_NAME <> 'source_mapping_revision' THEN
        RETURN NEW;
    END IF;
    SELECT revision, row_hash INTO p FROM app.source_mapping_revision
        WHERE tenant_id = NEW.tenant_id AND source_id = NEW.source_id
          AND account_ref = NEW.account_ref
        ORDER BY revision DESC LIMIT 1;
    IF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.row_hash THEN
        RAISE EXCEPTION 'app.source_map: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    -- each interval: exact keys, canonical instants, an account of this tenant,
    -- non-empty; sorted by valid_from and pairwise disjoint
    WITH iv AS (
        SELECT n, e, app.budget_book_instant(e -> 'valid_from') f,
               CASE WHEN e -> 'valid_to' = 'null'::jsonb THEN 'infinity'::timestamptz
                    ELSE app.budget_book_instant(e -> 'valid_to') END t
        FROM jsonb_array_elements(NEW.intervals) WITH ORDINALITY x (e, n))
    SELECT bool_or(NOT coalesce(jsonb_typeof(e) = 'object'
            AND e ?& ARRAY['account_id', 'valid_from', 'valid_to']
            AND e - ARRAY['account_id', 'valid_from', 'valid_to'] = '{}'::jsonb
            AND f IS NOT NULL AND t IS NOT NULL AND t > f
            AND EXISTS (SELECT 1 FROM app.account a WHERE a.tenant_id = NEW.tenant_id
                        AND to_jsonb(a.account_id) = e -> 'account_id'), false))
        OR EXISTS (SELECT 1 FROM iv a JOIN iv b ON a.n < b.n
                   WHERE b.f < a.f OR tstzrange(a.f, a.t) && tstzrange(b.f, b.t))
        INTO bad FROM iv;
    IF bad THEN
        RAISE EXCEPTION 'app.source_map: intervals' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.source_map_guard() FROM PUBLIC;
CREATE TRIGGER insert_guard BEFORE INSERT ON app.account
    FOR EACH ROW EXECUTE FUNCTION app.source_map_guard();
CREATE TRIGGER insert_guard BEFORE INSERT ON app.data_source
    FOR EACH ROW EXECUTE FUNCTION app.source_map_guard();
CREATE TRIGGER insert_guard BEFORE INSERT ON app.source_mapping_revision
    FOR EACH ROW EXECUTE FUNCTION app.source_map_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.account
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.data_source
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.source_mapping_revision
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.account ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.account
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.data_source ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.data_source
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.source_mapping_revision ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.source_mapping_revision
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.account, app.data_source, app.source_mapping_revision
    TO qw_app, qw_worker;
GRANT INSERT (tenant_id, account_id, name, broker, account_type, reporting_currency,
    analysis_scope, content_hash) ON app.account TO qw_app;
GRANT INSERT (tenant_id, source_id, kind, provider, content_hash)
    ON app.data_source TO qw_app;
GRANT INSERT (tenant_id, source_id, account_ref, revision, intervals,
    previous_hash, content_hash) ON app.source_mapping_revision TO qw_app;
