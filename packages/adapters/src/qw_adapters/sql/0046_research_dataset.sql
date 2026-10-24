-- Research dataset store (T032; owner decisions 70, 73 to 77 and 83, 2026-10-23).
-- One frozen dataset per tenant and id: `body` is the canonical domain
-- wire, `content_hash` its manifest hash (re-derived on every read by
-- qw_adapters.dataset_store), `terms` the research-rights terms (decision 77; not in
-- the content hash, covered by `event_hash`, re-checked at every load).
-- Decision 74: a different hash, body or terms under an id is `dataset_exists`; an
-- identical re-freeze stores nothing. Rights and retention are not checked here: a
-- direct insert of any CHECK-valid row is possible for qw_ingest; the load is the
-- gate. Decision 73: qw_ingest column INSERT; qw_worker and qw_assessor SELECT;
-- nothing for qw_app, qw_release or PUBLIC. qw_ingest also reads the five 0013
-- rights tables (rights are checked at freeze). The guard runs as the owner
-- (SECURITY DEFINER, fixed search_path, no EXECUTE grant) since qw_ingest cannot
-- read this table; forced RLS keeps it to the session tenant (an own-tenant oracle
-- only). Append-only, forced RLS, DB-clock window (60 s back, never ahead).

CREATE TABLE app.research_dataset (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    dataset_id text NOT NULL
        CHECK (dataset_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    body jsonb NOT NULL,
    terms jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, dataset_id),
    CONSTRAINT research_dataset_body CHECK (
        body - 'manifest' - 'facts' - 'universe' = '{}'::jsonb
        AND body -> 'manifest' ->> 'dataset_id' = dataset_id
        AND body -> 'manifest' ->> 'tenant_id' = tenant_id::text
        AND body -> 'manifest' ->> 'content_hash' = content_hash
        AND jsonb_typeof(body -> 'facts') = 'array'
        AND jsonb_typeof(body -> 'universe' -> 'events') = 'array'),
    CONSTRAINT research_dataset_terms CHECK (
        terms - 'scope' - 'jurisdiction' - 'retain_for_us' = '{}'::jsonb
        AND jsonb_typeof(terms -> 'scope') = 'string'
        AND jsonb_typeof(terms -> 'jurisdiction') = 'string'
        AND jsonb_typeof(terms -> 'retain_for_us') = 'number'
        AND (terms ->> 'retain_for_us') ~ '^[1-9][0-9]{0,17}$'
        AND (terms ->> 'retain_for_us')::numeric <= 3155760000000000)  -- 100 years
);

CREATE FUNCTION app.research_dataset_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    old_hash text;
    old_terms jsonb;
    old_body jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'research_dataset:' || NEW.tenant_id::text || ':' || NEW.dataset_id, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.research_dataset: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT content_hash, terms, body INTO old_hash, old_terms, old_body
        FROM app.research_dataset
        WHERE tenant_id = NEW.tenant_id AND dataset_id = NEW.dataset_id;
    IF FOUND THEN
        IF old_hash = NEW.content_hash AND old_terms = NEW.terms
           AND old_body = NEW.body THEN
            RETURN NULL;  -- decision 74: the identical freeze, already stored
        END IF;
        RAISE EXCEPTION 'app.research_dataset: dataset_exists'
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app.research_dataset_guard() FROM PUBLIC;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.research_dataset
    FOR EACH ROW EXECUTE FUNCTION app.research_dataset_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.research_dataset
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.research_dataset ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.research_dataset
    USING (tenant_id = app.current_tenant_id());

GRANT INSERT (tenant_id, dataset_id, content_hash, body, terms, recorded_at,
    event_hash) ON app.research_dataset TO qw_ingest;
GRANT SELECT ON app.research_dataset TO qw_worker, qw_assessor;
GRANT SELECT ON app.rights_provider, app.rights_feed, app.rights_feed_revision,
    app.rights_feed_transition, app.rights_entitlement TO qw_ingest;
