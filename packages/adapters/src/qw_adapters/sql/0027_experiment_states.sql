-- 0027: project contract 0.6.0 experiment states (owner decisions 1-3 and Q7,
-- 2026-10-20). 0026 stays as applied. New rows: a decision fixes the state
-- (insufficient_evidence and blocked are states; failed is no longer derived),
-- undecided rows are proposed or authorized, and code and optimizer stay blocked.
-- The replaced CHECKs are re-added NOT VALID: 0026 rows (failed for insufficient
-- evidence; blocked verdicts and code targets as proposed or authorized) are kept
-- and stay readable, and every new row is checked. parent_optimizer_version is
-- already nullable and set exactly for the optimizer (0026 check5, kept).
-- Revisions: proposed -> proposed, authorized, blocked; authorized -> authorized,
-- blocked, awaiting_review, rejected, insufficient_evidence; blocked -> any of
-- those and proposed; insufficient_evidence -> insufficient_evidence, blocked,
-- awaiting_review, rejected (re-assessment); awaiting_review, rejected and failed
-- are final here. A verdict id never repeats within an experiment.

ALTER TABLE app.experiment_revision
    DROP CONSTRAINT experiment_revision_state_check,
    DROP CONSTRAINT experiment_revision_check3,
    DROP CONSTRAINT experiment_revision_check4;

ALTER TABLE app.experiment_revision
    ADD CONSTRAINT experiment_revision_state_0027 CHECK (state IN ('proposed',
        'authorized', 'blocked', 'awaiting_review', 'rejected',
        'insufficient_evidence')) NOT VALID,
    ADD CONSTRAINT experiment_revision_decision_0027 CHECK (CASE decision
        WHEN 'accept_for_review' THEN state = 'awaiting_review'
        WHEN 'reject' THEN state = 'rejected'
        WHEN 'insufficient_evidence' THEN state = 'insufficient_evidence'
        WHEN 'blocked' THEN state = 'blocked'
        ELSE state IN ('proposed', 'authorized', 'blocked') END) NOT VALID,
    ADD CONSTRAINT experiment_revision_sandbox_0027 CHECK (CASE
        WHEN target IN ('application_code', 'recursive_optimizer')
        THEN state = 'blocked'
        ELSE state <> 'blocked' OR decision IS NOT DISTINCT FROM 'blocked' END)
        NOT VALID;

CREATE OR REPLACE FUNCTION app.experiment_revision_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    p app.experiment_revision%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.experiment_revision: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO p FROM app.experiment_revision
        WHERE tenant_id = NEW.tenant_id AND experiment_id = NEW.experiment_id
        ORDER BY revision DESC LIMIT 1;
    IF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at
       OR (p.revision IS NOT NULL AND (p.state, NEW.state) NOT IN (
           ('proposed', 'proposed'), ('proposed', 'authorized'),
           ('proposed', 'blocked'), ('authorized', 'authorized'),
           ('authorized', 'blocked'), ('authorized', 'awaiting_review'),
           ('authorized', 'rejected'), ('authorized', 'insufficient_evidence'),
           ('blocked', 'proposed'), ('blocked', 'authorized'),
           ('blocked', 'blocked'), ('blocked', 'awaiting_review'),
           ('blocked', 'rejected'), ('blocked', 'insufficient_evidence'),
           ('insufficient_evidence', 'insufficient_evidence'),
           ('insufficient_evidence', 'blocked'),
           ('insufficient_evidence', 'awaiting_review'),
           ('insufficient_evidence', 'rejected')))
       OR (p.revision IS NOT NULL AND (NEW.target, NEW.baseline_version,
           NEW.candidate_version, NEW.hypothesis, NEW.protocol_id,
           NEW.protected_assessor_version, NEW.data_snapshot_ids,
           NEW.budget_reservation_id, NEW.sandbox_profile_id, NEW.candidate_hash,
           NEW.parent_optimizer_version) IS DISTINCT FROM (p.target,
           p.baseline_version, p.candidate_version, p.hypothesis, p.protocol_id,
           p.protected_assessor_version, p.data_snapshot_ids,
           p.budget_reservation_id, p.sandbox_profile_id, p.candidate_hash,
           p.parent_optimizer_version))
       OR EXISTS (SELECT 1 FROM app.experiment_revision
                  WHERE tenant_id = NEW.tenant_id
                    AND experiment_id = NEW.experiment_id
                    AND verdict_id = NEW.verdict_id) THEN
        RAISE EXCEPTION 'app.experiment_revision: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM app.experiment_revision
               WHERE tenant_id = NEW.tenant_id
                 AND budget_reservation_id = NEW.budget_reservation_id
                 AND experiment_id <> NEW.experiment_id) THEN
        RAISE EXCEPTION 'app.experiment_revision: reservation of another experiment'
            USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END
$$;
