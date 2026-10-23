-- 0040: owner decisions 59 and 60 (2026-10-23), refining decision 42's re-run of
-- the assessor-run promotion (0038).
-- Decision 59: a plan's reserved read allows the reservation (attempt 1) and up to
-- three re-runs (attempts 2 to 4), each the next attempt and each repeating
-- attempt 1 exactly (verdict id, read and inputs digest); there is no attempt 5.
-- Decision 60: right after the evaluation, before any receipt, the run records a
-- digest of its trial outcome in app.holdout_run_outcome, once per plan, under the
-- attempt that evaluated; every later re-run is compared with it. A receipt on a
-- plan whose read is reserved now also needs that outcome. Read and written by
-- qw_assessor alone (decision 17). Append-only, forced RLS, tenant scope, DB-clock
-- window and the 0026 lock 'experiment:<tenant>', as in 0038.

ALTER TABLE app.holdout_run
    DROP CONSTRAINT holdout_run_attempt_check,
    ADD CONSTRAINT holdout_run_attempt_check CHECK (attempt BETWEEN 1 AND 4);

CREATE OR REPLACE FUNCTION app.holdout_run_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    first app.holdout_run%ROWTYPE;
    ok boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.holdout_run: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO first FROM app.holdout_run
        WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id AND attempt = 1;
    IF NEW.attempt = 1 THEN
        -- as 0038: this plan's read, by its protocol's assessor, recorded in this
        -- transaction, and no receipt has scored the plan's read before
        ok := first.attempt IS NULL
            AND EXISTS (SELECT 1 FROM app.holdout_access h
                JOIN app.assessor_protocol p
                  ON (p.tenant_id, p.experiment_id) = (h.tenant_id, NEW.experiment_id)
                WHERE h.tenant_id = NEW.tenant_id AND h.plan_id = NEW.plan_id
                  AND h.seq = NEW.seq AND h.access_id = NEW.access_id
                  AND p.plan_id = NEW.plan_id AND h.actor_id = p.assessor_actor_id
                  AND h.recorded_at >= transaction_timestamp())
            AND NOT EXISTS (SELECT 1 FROM app.evaluation_receipt
                WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id
                  AND scored_seq IS NOT NULL);
    ELSE  -- a re-run (decision 59): the next attempt, repeating attempt 1 exactly
        ok := first.attempt IS NOT NULL
            AND NEW.attempt = 1 + (SELECT max(attempt) FROM app.holdout_run
                WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id)
            AND (NEW.seq, NEW.experiment_id, NEW.candidate_id, NEW.verdict_id,
                 NEW.access_id, NEW.inputs_digest)
              = (first.seq, first.experiment_id, first.candidate_id,
                 first.verdict_id, first.access_id, first.inputs_digest);
    END IF;
    IF ok IS NOT TRUE THEN
        RAISE EXCEPTION 'app.holdout_run: not the reserved read'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TABLE app.holdout_run_outcome (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    plan_id text NOT NULL,
    attempt integer NOT NULL,
    outcome_digest text NOT NULL CHECK (outcome_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, plan_id),
    FOREIGN KEY (tenant_id, plan_id, attempt)
        REFERENCES app.holdout_run (tenant_id, plan_id, attempt)
);

CREATE FUNCTION app.holdout_run_outcome_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.holdout_run_outcome: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    -- recorded by the attempt that evaluated (the latest), before any receipt
    IF NEW.attempt IS DISTINCT FROM (SELECT max(attempt) FROM app.holdout_run
            WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id)
       OR EXISTS (SELECT 1 FROM app.evaluation_receipt
            WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id) THEN
        RAISE EXCEPTION 'app.holdout_run_outcome: not the evaluating attempt'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- As 0038, and a reserved plan's receipt follows its recorded outcome.
CREATE OR REPLACE FUNCTION app.holdout_run_receipt_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM app.holdout_run
               WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id
                 AND (experiment_id, candidate_id, verdict_id)
                     IS DISTINCT FROM
                     (NEW.experiment_id, NEW.candidate_id, NEW.verdict_id))
       OR (EXISTS (SELECT 1 FROM app.holdout_run
                   WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id)
           AND NOT EXISTS (SELECT 1 FROM app.holdout_run_outcome
                   WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id)) THEN
        RAISE EXCEPTION 'app.evaluation_receipt: the plan''s read is reserved'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.holdout_run_outcome
    FOR EACH ROW EXECUTE FUNCTION app.holdout_run_outcome_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.holdout_run_outcome
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.holdout_run_outcome ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.holdout_run_outcome
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.holdout_run_outcome TO qw_assessor;
GRANT INSERT (tenant_id, plan_id, attempt, outcome_digest, recorded_at, event_hash)
    ON app.holdout_run_outcome TO qw_assessor;
