-- The assessment request (T044; owner decisions 43, 64, 65 and 67, 2026-10-21 and
-- 2026-10-23).
-- One promotion request per tenant, experiment and verdict id, kept durably until
-- its run returns (a re-run repeats it exactly). `body` holds the caller-supplied
-- trial ledger and budget book in full (domain wire forms: decimal strings, UTC
-- instants), the strategy reference [id, version, implementation hash] (decision
-- 65) and every other input by content hash (decision 67). `request_hash` binds
-- the body to the tenant, experiment, verdict and recording time; the store
-- (qw_adapters.assessment_request_store) re-verifies it and re-parses the ledger
-- and book on every read. Decision 64: qw_worker inserts (it cannot read the row
-- back), qw_assessor may only read, and qw_app, qw_release and qw_ingest hold no
-- privilege. Append-only, forced RLS, tenant scope, DB-clock window (60 s back,
-- never ahead) as in 0028.

CREATE TABLE app.assessment_request (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    experiment_id text NOT NULL
        CHECK (experiment_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    verdict_id text NOT NULL
        CHECK (verdict_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, experiment_id, verdict_id),
    CHECK (body ->> 'experiment_id' = experiment_id
        AND body ->> 'verdict_id' = verdict_id
        AND body -> 'ledger' ->> 'tenant_id' = tenant_id::text
        AND jsonb_typeof(body -> 'book') = 'object'
        AND jsonb_typeof(body -> 'strategy') = 'array'
        AND jsonb_array_length(body -> 'strategy') = 3
        AND body -> 'strategy' ->> 2 ~ '^[0-9a-f]{64}$')
);

CREATE FUNCTION app.assessment_request_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.assessment_request: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.assessment_request
    FOR EACH ROW EXECUTE FUNCTION app.assessment_request_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.assessment_request
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.assessment_request ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.assessment_request
    USING (tenant_id = app.current_tenant_id());

GRANT INSERT (tenant_id, experiment_id, verdict_id, body, recorded_at, request_hash)
    ON app.assessment_request TO qw_worker;
GRANT SELECT ON app.assessment_request TO qw_assessor;
