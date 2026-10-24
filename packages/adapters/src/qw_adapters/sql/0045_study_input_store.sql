-- Study input store (T047, T033; owner decisions 70, 72 and 74, 2026-10-23).
-- Decision 70: durable stores for the inputs an assessment request binds only by
-- hash: the strategy search space and the evaluation protocol. Each row holds the
-- domain's strict wire (`body`), its `content_hash` and an `event_hash` binding
-- both to the tenant and the recording time; qw_adapters.study_store re-verifies
-- every row on read.
-- Decision 72: qw_worker records them (column INSERT only, content-addressed,
-- append-only); qw_assessor only reads (SELECT); qw_app, qw_ingest and qw_release
-- hold nothing. qw_worker gets no SELECT: no worker path reads them back (it binds
-- the hash of the value it holds, and an identical re-record is detected by the
-- insert itself). The protocol's `approved_by` stays caller-asserted and each row
-- is marked so: `approved_by_caller_asserted`, hashed by the store, and a CHECK
-- holds it true until a step-up approval flow exists (as 0042's marks).
-- Decision 74: one content per id. The primary key is (tenant, content_hash); a
-- unique index on (tenant, id) refuses a second, different freeze under the same
-- space or protocol id (`study_input_exists`). An identical re-record is a
-- no-op: only identical content conflicts on the primary key, which the store
-- reads as "already stored", so the first row and its recording time are kept.
-- (`ON CONFLICT (tenant_id, content_hash)` would need SELECT on those columns.)
-- Append-only, forced RLS, tenant scope, DB-clock window (60 s back, never ahead)
-- as in 0036 and 0041.

CREATE TABLE app.strategy_search_space (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    space_id text NOT NULL CHECK (space_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT strategy_search_space_pkey PRIMARY KEY (tenant_id, content_hash),
    CONSTRAINT strategy_search_space_one_content_per_id UNIQUE (tenant_id, space_id),
    CHECK (body ->> 'space_id' = space_id
        AND jsonb_typeof(body -> 'dimensions') = 'array')
);

CREATE TABLE app.evaluation_protocol (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    protocol_id text NOT NULL
        CHECK (protocol_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    body jsonb NOT NULL,
    approved_by_caller_asserted boolean NOT NULL
        CONSTRAINT evaluation_protocol_approved_by_caller_asserted
        CHECK (approved_by_caller_asserted IS TRUE),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT evaluation_protocol_pkey PRIMARY KEY (tenant_id, content_hash),
    CONSTRAINT evaluation_protocol_one_content_per_id UNIQUE (tenant_id, protocol_id),
    CHECK (body ->> 'protocol_id' = protocol_id
        AND jsonb_typeof(body -> 'approved_by') = 'string')
);

CREATE FUNCTION app.study_input_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock',
            TG_TABLE_NAME, NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.strategy_search_space
    FOR EACH ROW EXECUTE FUNCTION app.study_input_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.strategy_search_space
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.strategy_search_space
    ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.strategy_search_space
    USING (tenant_id = app.current_tenant_id());

CREATE TRIGGER insert_guard BEFORE INSERT ON app.evaluation_protocol
    FOR EACH ROW EXECUTE FUNCTION app.study_input_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.evaluation_protocol
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.evaluation_protocol
    ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.evaluation_protocol
    USING (tenant_id = app.current_tenant_id());

GRANT INSERT (tenant_id, space_id, content_hash, body, recorded_at, event_hash)
    ON app.strategy_search_space TO qw_worker;
GRANT INSERT (tenant_id, protocol_id, content_hash, body,
    approved_by_caller_asserted, recorded_at, event_hash)
    ON app.evaluation_protocol TO qw_worker;
GRANT SELECT ON app.strategy_search_space, app.evaluation_protocol TO qw_assessor;
