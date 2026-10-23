-- 0036: owner decisions 41, 51, 52, 53 and 54 (2026-10-21 to 2026-10-23).
-- The protected holdout price series the assessor evaluates a promotion on: the
-- holdout series and the baseline series (decision 51), keyed on tenant and plan
-- (decision 53), exactly one per plan, instrument and role (decision 52; a
-- mistake is fixed with a new plan). `series_role` is 'holdout' or the baseline's
-- series id (the key the frozen evaluation protocol names it by), and a baseline
-- row's series carries that id. Readable and writable only by qw_assessor
-- (decision 41: the operator loads it through the assessor login); qw_app,
-- qw_worker, qw_ingest and qw_release (the release operator, 0035) hold no
-- privilege. Preregistered: a row is
-- refused once any stored assessor protocol of the tenant names its plan
-- (`plan_frozen`), checked under the 0026 lock 'experiment:<tenant>' that a
-- protocol insert also takes, so a load cannot race the freeze. Append-only,
-- forced RLS, tenant scope, DB-clock window (60 s back, never ahead) as in 0028.
-- `body` is the domain PriceSeries wire (decimal strings, UTC instants);
-- `content_hash` is its digest and `event_hash` binds it to the tenant, plan,
-- role and recording time, both re-verified by qw_adapters.holdout_series_store.

CREATE TABLE app.holdout_series (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    plan_id text NOT NULL CHECK (plan_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    instrument_id uuid NOT NULL,
    series_role text NOT NULL
        CHECK (series_role ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    series_id text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, plan_id, instrument_id, series_role),
    CHECK (body ->> 'series_id' = series_id
        AND body ->> 'tenant_id' = tenant_id::text
        AND body ->> 'instrument' = instrument_id::text
        AND jsonb_typeof(body -> 'bars') = 'array'
        AND (series_role = 'holdout' OR series_role = series_id))
);
-- Decision 54 (2026-10-23): at most one row per plan and baseline, whatever the
-- instrument, so a second load of a baseline is refused when it is made, before
-- the freeze, rather than at the read.
CREATE UNIQUE INDEX holdout_series_one_baseline
    ON app.holdout_series (tenant_id, plan_id, series_role)
    WHERE series_role <> 'holdout';

CREATE FUNCTION app.holdout_series_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.holdout_series: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM app.assessor_protocol
               WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id) THEN
        RAISE EXCEPTION 'app.holdout_series: plan_frozen'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.holdout_series
    FOR EACH ROW EXECUTE FUNCTION app.holdout_series_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.holdout_series
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.holdout_series ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.holdout_series
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.holdout_series TO qw_assessor;
GRANT INSERT (tenant_id, plan_id, instrument_id, series_role, series_id,
    content_hash, body, recorded_at, event_hash) ON app.holdout_series TO qw_assessor;
