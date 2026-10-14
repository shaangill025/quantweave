-- 0011: calculation receipts (T054 increment 2; spec §5 numeric-function outputs;
-- T008 C-15). Mirrors qw_domain.calculation.CalculationRecord; the adapter is
-- qw_adapters.calculation_store, read by GET /calculations/{calculation_id}.
-- - `record` is the domain's canonical record (decimal strings, UTC `Z` times; it
--   excludes computed_at), `content_hash` its SHA-256 and `id` 'calc:' + that hash,
--   so the same figure from the same inputs is one row per tenant. The adapter
--   recomputes the hash and re-checks the domain rules on every load.
-- - Append-only: runtime roles get SELECT and column INSERT only, and statement
--   triggers refuse UPDATE, DELETE and TRUNCATE (as in 0007). A repeated insert of
--   the same id is ignored, so the first computed_at is kept, never reset.
-- No SECURITY DEFINER function: every check runs as the inserting role under RLS.

CREATE TABLE app.calculation_record (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    id text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'
        AND octet_length(record::text) <= 262144),
    computed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    CHECK (id = 'calc:' || content_hash)
);

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.calculation_record
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.calculation_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.calculation_record FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.calculation_record
    USING (tenant_id = app.current_tenant_id());

-- Figures are computed by API requests (qw_app) and jobs (qw_worker).
GRANT SELECT, INSERT (tenant_id, id, content_hash, record, computed_at)
    ON app.calculation_record TO qw_app, qw_worker;
