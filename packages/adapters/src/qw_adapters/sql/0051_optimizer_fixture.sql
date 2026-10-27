-- T050 increment 8. `optimizer_promotion_fixture` (decisions 41, 87): the plan and
-- its promotion fixture, one row per installation and plan, preregistered before
-- a logged optimizer protocol names the plan (`plan_frozen`, under the registry's
-- `optimizer:<installation>` lock; READ COMMITTED asserted, so the check after the
-- lock sees a protocol committed meanwhile). qw_assessor alone reads and writes
-- it; as 0047/0049: forced RLS, append-only, DB clock, store hash on read. The
-- guard runs as the invoker (as 0036): its one writer reads both tables.
-- Cycles carry the protocol they ran under; a definer guard refuses one that is
-- not the experiment's logged protocol (`protocol_mismatch`). Decision 97: the
-- worker reads the bootstrap tenant's one column (the `optimizer.cycle` job).

CREATE TABLE app.optimizer_promotion_fixture (
    installation_id text NOT NULL
        CHECK (installation_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    plan_id text NOT NULL CHECK (plan_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    fixture_hash text NOT NULL CHECK (fixture_hash ~ '^[0-9a-f]{64}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (installation_id, plan_id),
    CONSTRAINT optimizer_promotion_fixture_body CHECK ((
        body - 'plan' - 'fixture' = '{}'::jsonb
        AND body -> 'plan' ->> 'plan_id' = plan_id
        AND body -> 'plan' ->> 'protocol_hash' = fixture_hash
        AND body -> 'fixture' ->> 'split' = 'promotion') IS TRUE)
);

CREATE FUNCTION app.optimizer_promotion_fixture_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'optimizer:' || NEW.installation_id, 0));
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'app.optimizer_promotion_fixture: isolation'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.optimizer_promotion_fixture: clock'
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM app.optimizer_registry_entry p
               WHERE p.installation_id = NEW.installation_id AND p.kind = 'protocol'
                 AND p.body ->> 'plan_id' = NEW.plan_id) THEN
        RAISE EXCEPTION 'app.optimizer_promotion_fixture: plan_frozen'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.optimizer_promotion_fixture_guard() FROM PUBLIC;
CREATE TRIGGER insert_guard BEFORE INSERT ON app.optimizer_promotion_fixture
    FOR EACH ROW EXECUTE FUNCTION app.optimizer_promotion_fixture_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.optimizer_promotion_fixture
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.optimizer_promotion_fixture ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY installation_assessor ON app.optimizer_promotion_fixture
    TO qw_assessor USING (true) WITH CHECK (true);

GRANT SELECT ON app.optimizer_promotion_fixture TO qw_assessor;
GRANT INSERT (installation_id, plan_id, fixture_hash, body, recorded_at, event_hash)
    ON app.optimizer_promotion_fixture TO qw_assessor;

GRANT SELECT (tenant_id) ON app.installation_bootstrap TO qw_worker;

ALTER TABLE app.optimizer_cycle ADD COLUMN protocol jsonb;
GRANT INSERT (protocol) ON app.optimizer_cycle TO qw_worker;

CREATE FUNCTION app.optimizer_cycle_protocol_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
BEGIN  -- after 0047's insert_guard (trigger name order), which checks the runner
    IF NEW.protocol IS DISTINCT FROM (SELECT e.body FROM app.optimizer_registry_entry e
            WHERE e.installation_id = NEW.installation_id AND e.kind = 'protocol'
              AND e.entry_id = NEW.experiment_id) THEN
        RAISE EXCEPTION 'app.optimizer_cycle: protocol_mismatch'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.optimizer_cycle_protocol_guard() FROM PUBLIC;
CREATE TRIGGER protocol_guard BEFORE INSERT ON app.optimizer_cycle
    FOR EACH ROW EXECUTE FUNCTION app.optimizer_cycle_protocol_guard();
