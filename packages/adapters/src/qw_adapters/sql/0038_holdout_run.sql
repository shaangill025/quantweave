-- 0038: owner decision 42 (2026-10-21), the assessor-run promotion of decision 39.
-- The assessor records its one promotion read before it opens any data (the
-- write-ahead reservation): the read is the plan's seq-1 app.holdout_access row
-- (0028), and app.holdout_run records attempt 1 under it in the same transaction.
-- After a crash between that reservation and the verdict, decision 42 allows one
-- deterministic re-run under the same recorded read, candidate and protocol, with
-- no new read: attempt 2, which must repeat attempt 1 exactly (verdict id, read
-- and the digest of every run input). There is no attempt 3. Once a plan's read is
-- reserved, only that run's verdict may be stored as a receipt on the plan. Read
-- and written by qw_assessor alone (decision 17); qw_app, qw_worker, qw_ingest and
-- qw_release hold no privilege. Append-only, forced RLS, tenant scope, DB-clock
-- window and the 0026 lock 'experiment:<tenant>', as in 0028.

CREATE TABLE app.holdout_run (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    plan_id text NOT NULL,
    attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 2),
    seq integer NOT NULL CHECK (seq = 1),
    experiment_id text NOT NULL,
    candidate_id text NOT NULL,
    verdict_id text NOT NULL CHECK (verdict_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    access_id text NOT NULL,
    inputs_digest text NOT NULL CHECK (inputs_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, plan_id, attempt),
    FOREIGN KEY (tenant_id, plan_id, seq)
        REFERENCES app.holdout_access (tenant_id, plan_id, seq),
    FOREIGN KEY (tenant_id, plan_id, access_id)
        REFERENCES app.holdout_access (tenant_id, plan_id, access_id),
    FOREIGN KEY (tenant_id, experiment_id)
        REFERENCES app.assessor_protocol (tenant_id, experiment_id)
);

CREATE FUNCTION app.holdout_run_guard() RETURNS trigger
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
        -- the reserved read is this plan's, by its protocol's assessor, recorded
        -- in this transaction (its DB-clock time is not before the transaction
        -- began: an earlier, caller-recorded read is never reserved), and no
        -- receipt has scored the plan's read before the reservation
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
    ELSE  -- the one re-run repeats attempt 1 exactly (decision 42)
        ok := NEW.attempt = 2 AND first.attempt IS NOT NULL
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

-- A receipt on a plan whose read is reserved is the run's verdict, or nothing.
CREATE FUNCTION app.holdout_run_receipt_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM app.holdout_run
               WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id
                 AND (experiment_id, candidate_id, verdict_id)
                     IS DISTINCT FROM
                     (NEW.experiment_id, NEW.candidate_id, NEW.verdict_id)) THEN
        RAISE EXCEPTION 'app.evaluation_receipt: the plan''s read is reserved'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.holdout_run
    FOR EACH ROW EXECUTE FUNCTION app.holdout_run_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.holdout_run
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER reserved_read_guard BEFORE INSERT ON app.evaluation_receipt
    FOR EACH ROW EXECUTE FUNCTION app.holdout_run_receipt_guard();
ALTER TABLE app.holdout_run ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.holdout_run
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.holdout_run TO qw_assessor;
GRANT INSERT (tenant_id, plan_id, attempt, seq, experiment_id, candidate_id,
    verdict_id, access_id, inputs_digest, recorded_at, event_hash)
    ON app.holdout_run TO qw_assessor;
