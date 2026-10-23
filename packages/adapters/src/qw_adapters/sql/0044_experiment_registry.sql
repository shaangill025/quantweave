-- The experiment registry log (T044; owner decisions 71 and 79 to 81, 2026-10-23).
-- An append-only, hash-chained log per tenant of `qw_domain.improvement`
-- registry entries (protocol, candidate, verdict), each `body` the entry's domain
-- `wire()`. The chain is per tenant: the domain registry is per scope, and the
-- stores keep tenant scope only (scope_id = the tenant); candidate ids are unique
-- per tenant (decision 80). `event_hash` binds the tenant, seq, kind, entry id,
-- body, recording time and `prev_hash`; the store
-- (qw_adapters.experiment_registry_store) replays the log through the domain
-- registry before each append and on every read, and the assessor replays it as
-- of a request's `recorded_at`.
-- Decisions 79 and 81: qw_assessor alone reads the log and appends protocols and
-- candidates (at `assessor_store.bind`); qw_worker appends verdicts only, blind:
-- the assessor-side step replays the log and hands it the head, and this guard
-- refuses an insert that does not extend that exact head (seq n + 1 and the
-- head's hash, time never backwards), within the DB-clock window (60 s back,
-- never ahead), under the `experiment:<tenant>` lock. The guard runs as the
-- owner (qw_migrate, SECURITY DEFINER, fixed search_path, no EXECUTE grant), so
-- qw_worker needs no SELECT; forced RLS still scopes its read to the session
-- tenant, it returns nothing, and a refusal names no stored value (as 0034).
-- Restrictive policies hold each role to the kinds it writes. qw_app, qw_release
-- and qw_ingest hold nothing. Append-only, forced RLS.

CREATE TABLE app.experiment_registry_entry (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    seq bigint NOT NULL CHECK (seq >= 1),
    kind text NOT NULL CHECK (kind IN ('protocol', 'candidate', 'verdict')),
    entry_id text NOT NULL
        CHECK (entry_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    prev_hash text CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, kind, entry_id),
    CHECK (body ->> CASE kind WHEN 'protocol' THEN 'experiment_id'
        WHEN 'candidate' THEN 'candidate_id' ELSE 'verdict_id' END = entry_id)
);

CREATE FUNCTION app.experiment_registry_entry_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    p app.experiment_registry_entry%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.experiment_registry_entry: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO p FROM app.experiment_registry_entry
        WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    IF NEW.seq <> coalesce(p.seq, 0) + 1
       OR NEW.prev_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at THEN
        RAISE EXCEPTION 'app.experiment_registry_entry: not the head handed over'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app.experiment_registry_entry_guard() FROM PUBLIC;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.experiment_registry_entry
    FOR EACH ROW EXECUTE FUNCTION app.experiment_registry_entry_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.experiment_registry_entry
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.experiment_registry_entry ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.experiment_registry_entry
    USING (tenant_id = app.current_tenant_id());
CREATE POLICY worker_verdicts ON app.experiment_registry_entry AS RESTRICTIVE
    FOR INSERT TO qw_worker WITH CHECK (kind = 'verdict');
CREATE POLICY assessor_entries ON app.experiment_registry_entry AS RESTRICTIVE
    FOR INSERT TO qw_assessor WITH CHECK (kind IN ('protocol', 'candidate'));

GRANT INSERT (tenant_id, seq, kind, entry_id, body, recorded_at, prev_hash,
    event_hash) ON app.experiment_registry_entry TO qw_worker, qw_assessor;
GRANT SELECT ON app.experiment_registry_entry TO qw_assessor;
