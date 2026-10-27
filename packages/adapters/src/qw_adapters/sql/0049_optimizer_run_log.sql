-- The optimizer trial's installation run log (T050; decisions 42, 59, 60, 87): the
-- installation-scope 0038/0040. qw_assessor alone reads and writes it (decision
-- 17). As 0047: forced RLS, append-only, DB clock, store hashes re-verified on
-- read; hardened definer guards, each under the `optimizer:<installation>` lock.
-- - `optimizer_run` (decision 42's write-ahead read): attempt 1 names a logged
--   protocol of the plan and its candidate (`not_logged`); each attempt is the
--   next (`attempt_moved`), repeats attempt 1 (`rerun_mismatch`), at most four
--   (`rerun_used`, decision 59), none once a verdict of its id or candidate is
--   logged (`run_recorded`), never back in time (`back_dated`).
-- - `optimizer_run_outcome` (decision 60): once per plan, by the latest attempt
--   (`not_evaluating`), not before it (`back_dated`).
-- - Verdicts: on a reserved plan only the run's, after its outcome
--   (`run_reserved`, as 0040); with no run only BLOCKED or INSUFFICIENT
--   (`not_reserved`).

CREATE TABLE app.optimizer_run (
    installation_id text NOT NULL
        CHECK (installation_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    plan_id text NOT NULL CHECK (plan_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 4),
    experiment_id text NOT NULL,
    candidate_id text NOT NULL,
    verdict_id text NOT NULL CHECK (verdict_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    fixture_hash text NOT NULL CHECK (fixture_hash ~ '^[0-9a-f]{64}$'),
    inputs_digest text NOT NULL CHECK (inputs_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (installation_id, plan_id, attempt)
);

CREATE TABLE app.optimizer_run_outcome (
    installation_id text NOT NULL,
    plan_id text NOT NULL,
    attempt integer NOT NULL,
    outcome_digest text NOT NULL CHECK (outcome_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (installation_id, plan_id),
    FOREIGN KEY (installation_id, plan_id, attempt)
        REFERENCES app.optimizer_run (installation_id, plan_id, attempt)
);

CREATE FUNCTION app.optimizer_run_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    first app.optimizer_run%ROWTYPE;
    latest integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'optimizer:' || NEW.installation_id, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.optimizer_run: clock' USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM app.optimizer_registry_entry v
               WHERE v.installation_id = NEW.installation_id AND v.kind = 'verdict'
                 AND (v.entry_id = NEW.verdict_id
                      OR v.body ->> 'candidate_id' = NEW.candidate_id)) THEN
        RAISE EXCEPTION 'app.optimizer_run: run_recorded'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.recorded_at < (SELECT max(recorded_at) FROM app.optimizer_run
            WHERE installation_id = NEW.installation_id AND plan_id = NEW.plan_id) THEN
        RAISE EXCEPTION 'app.optimizer_run: back_dated'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO first FROM app.optimizer_run
        WHERE installation_id = NEW.installation_id AND plan_id = NEW.plan_id
          AND attempt = 1;
    SELECT max(attempt) INTO latest FROM app.optimizer_run
        WHERE installation_id = NEW.installation_id AND plan_id = NEW.plan_id;
    IF latest >= 4 THEN
        RAISE EXCEPTION 'app.optimizer_run: rerun_used'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.attempt <> coalesce(latest, 0) + 1 THEN
        RAISE EXCEPTION 'app.optimizer_run: attempt_moved'
            USING ERRCODE = 'check_violation';
    END IF;
    IF first.attempt IS NULL THEN  -- the reservation: the plan's logged protocol
        IF NOT EXISTS (SELECT 1 FROM app.optimizer_registry_entry p
               WHERE p.installation_id = NEW.installation_id AND p.kind = 'protocol'
                 AND p.entry_id = NEW.experiment_id
                 AND p.body ->> 'plan_id' = NEW.plan_id)
           OR NOT EXISTS (SELECT 1 FROM app.optimizer_registry_entry c
               WHERE c.installation_id = NEW.installation_id AND c.kind = 'candidate'
                 AND c.entry_id = NEW.candidate_id
                 AND c.body ->> 'experiment_id' = NEW.experiment_id) THEN
            RAISE EXCEPTION 'app.optimizer_run: not_logged'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF (NEW.experiment_id, NEW.candidate_id, NEW.verdict_id, NEW.fixture_hash,
           NEW.inputs_digest)
          IS DISTINCT FROM (first.experiment_id, first.candidate_id,
           first.verdict_id, first.fixture_hash, first.inputs_digest) THEN
        RAISE EXCEPTION 'app.optimizer_run: rerun_mismatch'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION app.optimizer_run_outcome_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'optimizer:' || NEW.installation_id, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.optimizer_run_outcome: clock'
            USING ERRCODE = 'check_violation';
    END IF;
    -- recorded by the attempt that evaluated (the latest); the run's verdict
    -- needs this outcome, and the plan holds one (primary key)
    IF NEW.attempt IS DISTINCT FROM (SELECT max(attempt) FROM app.optimizer_run
            WHERE installation_id = NEW.installation_id AND plan_id = NEW.plan_id)
    THEN
        RAISE EXCEPTION 'app.optimizer_run_outcome: not_evaluating'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.recorded_at < (SELECT recorded_at FROM app.optimizer_run
            WHERE installation_id = NEW.installation_id AND plan_id = NEW.plan_id
              AND attempt = NEW.attempt) THEN
        RAISE EXCEPTION 'app.optimizer_run_outcome: back_dated'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- A verdict on a plan whose read is reserved is the run's, after its outcome.
CREATE FUNCTION app.optimizer_run_verdict_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    plan text;
BEGIN
    IF NEW.kind <> 'verdict' THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'optimizer:' || NEW.installation_id, 0));  -- not by trigger-name order
    SELECT p.body ->> 'plan_id' INTO plan FROM app.optimizer_registry_entry p
        WHERE p.installation_id = NEW.installation_id AND p.kind = 'protocol'
          AND p.entry_id = NEW.body ->> 'experiment_id';
    IF EXISTS (SELECT 1 FROM app.optimizer_run r
               WHERE r.installation_id = NEW.installation_id AND r.plan_id = plan
                 AND (r.experiment_id, r.candidate_id, r.verdict_id)
                     IS DISTINCT FROM (NEW.body ->> 'experiment_id',
                     NEW.body ->> 'candidate_id', NEW.entry_id))
       OR (EXISTS (SELECT 1 FROM app.optimizer_run r
                   WHERE r.installation_id = NEW.installation_id AND r.plan_id = plan)
           AND NOT EXISTS (SELECT 1 FROM app.optimizer_run_outcome o
                   WHERE o.installation_id = NEW.installation_id
                     AND o.plan_id = plan)) THEN
        RAISE EXCEPTION 'app.optimizer_registry_entry: run_reserved'
            USING ERRCODE = 'check_violation';
    END IF;
    -- every protocol here is an optimizer's (0047): only a run decides
    IF coalesce(NEW.body ->> 'decision', '') NOT IN ('blocked', 'insufficient_evidence')
       AND NOT EXISTS (SELECT 1 FROM app.optimizer_run r
               WHERE r.installation_id = NEW.installation_id AND r.plan_id = plan) THEN
        RAISE EXCEPTION 'app.optimizer_registry_entry: not_reserved'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.optimizer_run_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION app.optimizer_run_outcome_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION app.optimizer_run_verdict_guard() FROM PUBLIC;
CREATE TRIGGER run_guard BEFORE INSERT ON app.optimizer_registry_entry
    FOR EACH ROW EXECUTE FUNCTION app.optimizer_run_verdict_guard();

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['optimizer_run', 'optimizer_run_outcome'] LOOP
        EXECUTE format('CREATE TRIGGER insert_guard BEFORE INSERT ON app.%I '
            'FOR EACH ROW EXECUTE FUNCTION app.%I()', t, t || '_guard');
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR '
            'TRUNCATE ON app.%I FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.journal_append_only()', t);
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY, '
            'FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY installation_guard ON app.%I FOR SELECT '
            'TO qw_migrate USING (true)', t);
        EXECUTE format('CREATE POLICY installation_assessor ON app.%I '
            'TO qw_assessor USING (true) WITH CHECK (true)', t);
        EXECUTE format('GRANT SELECT ON app.%I TO qw_assessor', t);
    END LOOP;
END
$$;

GRANT INSERT (installation_id, plan_id, attempt, experiment_id, candidate_id,
    verdict_id, fixture_hash, inputs_digest, recorded_at, event_hash)
    ON app.optimizer_run TO qw_assessor;
GRANT INSERT (installation_id, plan_id, attempt, outcome_digest, recorded_at,
    event_hash) ON app.optimizer_run_outcome TO qw_assessor;
