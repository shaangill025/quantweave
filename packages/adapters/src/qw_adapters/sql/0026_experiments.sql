-- 0026: improvement experiments (T044 increment 2; listExperiments, readExperiment;
-- spec §11), replayed by qw_adapters.experiment_store (fail closed). Append-only
-- revisions 1..n, each naming the previous hash (the store writes only what
-- `improvement.record` derives; a raw qw_worker INSERT is not checked for that).
-- Enforced here: identity columns never change; authorized never returns to
-- proposed and nothing follows a decision; the decision fixes the state; code and
-- optimizer stay proposed. Tenant scope only (no installation role yet, T008 §5);
-- a reservation id names one experiment. Times: DB clock (60 s back, never ahead),
-- never before the previous revision, under lock 'experiment:<tenant>' (taken last).
-- qw_worker records; qw_app only reads (no create operation is served).

CREATE TABLE app.experiment_revision (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    experiment_id text NOT NULL
        CHECK (experiment_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    revision integer NOT NULL CHECK (revision BETWEEN 1 AND 2147483646),
    target text NOT NULL CHECK (target IN ('personal_adaptation', 'research',
        'strategy', 'evaluator', 'application_code', 'recursive_optimizer')),
    scope text NOT NULL CHECK (scope = 'tenant'),
    scope_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('proposed', 'authorized', 'awaiting_review',
        'rejected', 'failed')),
    baseline_version text NOT NULL,
    candidate_version text NOT NULL,
    hypothesis text NOT NULL CHECK (char_length(hypothesis) BETWEEN 1 AND 4000),
    protocol_id text NOT NULL CHECK (protocol_id ~ '^[0-9a-f]{64}$'),
    protected_assessor_version text NOT NULL,
    data_snapshot_ids text[] NOT NULL CHECK (cardinality(data_snapshot_ids) <= 64),
    budget_reservation_id text NOT NULL,
    sandbox_profile_id text NOT NULL,
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    parent_optimizer_version text,
    verdict_id text,
    decision text CHECK (decision IN ('accept_for_review', 'reject',
        'insufficient_evidence', 'blocked')),
    reasons text[] NOT NULL CHECK (cardinality(reasons) <= 64),
    recorded_at timestamptz NOT NULL,
    previous_hash text CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, experiment_id, revision),
    CHECK (scope_id = tenant_id::text),
    CHECK ((revision = 1) = (previous_hash IS NULL)),
    CHECK ((verdict_id IS NULL) = (decision IS NULL)),
    CHECK (CASE decision WHEN 'accept_for_review' THEN state = 'awaiting_review'
        WHEN 'reject' THEN state = 'rejected'
        WHEN 'insufficient_evidence' THEN state = 'failed'
        ELSE state IN ('proposed', 'authorized') END),
    CHECK (target NOT IN ('application_code', 'recursive_optimizer')
        OR state = 'proposed'),
    CHECK ((target = 'recursive_optimizer') = (parent_optimizer_version IS NOT NULL))
);

CREATE FUNCTION app.experiment_revision_guard() RETURNS trigger
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
       OR p.state IN ('awaiting_review', 'rejected', 'failed')
       OR (p.state = 'authorized' AND NEW.state = 'proposed')
       OR (p.revision IS NOT NULL AND (NEW.target, NEW.baseline_version,
           NEW.candidate_version, NEW.hypothesis, NEW.protocol_id,
           NEW.protected_assessor_version, NEW.data_snapshot_ids,
           NEW.budget_reservation_id, NEW.sandbox_profile_id, NEW.candidate_hash,
           NEW.parent_optimizer_version) IS DISTINCT FROM (p.target,
           p.baseline_version, p.candidate_version, p.hypothesis, p.protocol_id,
           p.protected_assessor_version, p.data_snapshot_ids,
           p.budget_reservation_id, p.sandbox_profile_id, p.candidate_hash,
           p.parent_optimizer_version)) THEN
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

CREATE TRIGGER insert_guard BEFORE INSERT ON app.experiment_revision
    FOR EACH ROW EXECUTE FUNCTION app.experiment_revision_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.experiment_revision
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.experiment_revision ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.experiment_revision
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.experiment_revision TO qw_app, qw_worker;
GRANT INSERT (tenant_id, experiment_id, revision, target, scope, scope_id, state,
    baseline_version, candidate_version, hypothesis, protocol_id,
    protected_assessor_version, data_snapshot_ids, budget_reservation_id,
    sandbox_profile_id, candidate_hash, parent_optimizer_version, verdict_id,
    decision, reasons, recorded_at, previous_hash, event_hash)
    ON app.experiment_revision TO qw_worker;
