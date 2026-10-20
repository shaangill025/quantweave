-- 0028: T044 assessor store (owner decisions Q7, Q3), written as qw_worker through
-- qw_adapters.assessor_store and re-verified through the domain on every read.
-- Append-only, forced RLS, tenant scope, DB-clock window (60 s back, never ahead),
-- the single lock 'experiment:<tenant>' of 0026. Tables: the frozen protocol; the
-- recorded candidate's lineage (depth 0 from the baseline); a plan's granted
-- holdout reads (seq 1..n); evaluation receipts, bound to the stored protocol,
-- candidate and assessor, a scored one referencing its read (seq 1), and at most
-- one scored receipt per plan. New experiment_revision rows name a stored protocol
-- and, when decided, their receipt (NOT VALID keeps 0026/0027 rows). qw_app may
-- read only the receipts' non-protected columns.

CREATE TABLE app.assessor_protocol (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    experiment_id text NOT NULL
        CHECK (experiment_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    protocol_hash text NOT NULL CHECK (protocol_hash ~ '^[0-9a-f]{64}$'),
    plan_id text NOT NULL,
    baseline_version text NOT NULL,
    assessor_actor_id text NOT NULL,
    assessor_route_id text NOT NULL,
    assessor_family_id text NOT NULL,
    assessor_version text NOT NULL,
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, experiment_id),
    UNIQUE (tenant_id, experiment_id, protocol_hash),
    CHECK (body ->> 'experiment_id' = experiment_id
        AND body ->> 'scope' = 'tenant' AND body ->> 'scope_id' = tenant_id::text
        AND body ->> 'plan_id' = plan_id
        AND body ->> 'baseline_version' = baseline_version
        AND body -> 'assessor' = jsonb_build_array(assessor_actor_id,
            assessor_route_id, assessor_family_id, assessor_version))
);

CREATE TABLE app.assessor_candidate (
    tenant_id uuid NOT NULL,
    experiment_id text NOT NULL,
    candidate_id text NOT NULL,
    depth integer NOT NULL CHECK (depth BETWEEN 0 AND 10000),
    parent_id text NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, experiment_id, candidate_id),
    UNIQUE (tenant_id, experiment_id, depth),
    FOREIGN KEY (tenant_id, experiment_id)
        REFERENCES app.assessor_protocol (tenant_id, experiment_id),
    CHECK (body ->> 'candidate_id' = candidate_id
        AND body ->> 'experiment_id' = experiment_id
        AND body ->> 'parent_id' = parent_id)
);

CREATE TABLE app.holdout_access (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    plan_id text NOT NULL,
    seq integer NOT NULL CHECK (seq BETWEEN 1 AND 1000000),
    access_id text NOT NULL,
    trial_id text NOT NULL,
    family text NOT NULL,
    actor_id text NOT NULL,
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, plan_id, seq),
    UNIQUE (tenant_id, plan_id, access_id),
    -- the domain Access wire: a granted promotion read by the protected assessor
    CHECK (body ->> 0 = access_id AND body ->> 1 = plan_id AND body ->> 2 = trial_id
        AND body -> 3 ->> 0 = family AND body ->> 4 = 'promotion'
        AND body ->> 7 = actor_id AND body ->> 8 = 'protected_assessor'
        AND body -> 10 = 'true'::jsonb AND body -> 12 = to_jsonb(seq))
);

CREATE TABLE app.evaluation_receipt (
    tenant_id uuid NOT NULL,
    experiment_id text NOT NULL,
    verdict_id text NOT NULL,
    candidate_id text NOT NULL,
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    protocol_hash text NOT NULL CHECK (protocol_hash ~ '^[0-9a-f]{64}$'),
    assessor_actor_id text NOT NULL,
    assessor_route_id text NOT NULL,
    assessor_family_id text NOT NULL,
    assessor_version text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('accept_for_review', 'reject',
        'insufficient_evidence', 'blocked')),
    reasons text[] NOT NULL CHECK (cardinality(reasons) <= 256),
    scores jsonb NOT NULL,
    trial_ids text[] NOT NULL CHECK (cardinality(trial_ids) <= 1),
    failed_trial_ids text[] NOT NULL,
    limitations text[] NOT NULL,
    assessed_at timestamptz NOT NULL,
    plan_id text NOT NULL,
    scored_seq integer CHECK (scored_seq = 1),
    ledger_entries integer CHECK (ledger_entries >= 1),
    ledger_digest text CHECK (ledger_digest ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, experiment_id, verdict_id),
    UNIQUE (tenant_id, experiment_id, verdict_id, decision),
    FOREIGN KEY (tenant_id, experiment_id)
        REFERENCES app.assessor_protocol (tenant_id, experiment_id),
    FOREIGN KEY (tenant_id, experiment_id, candidate_id)
        REFERENCES app.assessor_candidate (tenant_id, experiment_id, candidate_id),
    FOREIGN KEY (tenant_id, plan_id, scored_seq)
        REFERENCES app.holdout_access (tenant_id, plan_id, seq),
    CHECK ((decision = 'accept_for_review') = (cardinality(reasons) = 0)),
    CHECK ((scored_seq IS NULL) = (cardinality(trial_ids) = 0)),
    CHECK (decision NOT IN ('accept_for_review', 'reject') OR scored_seq IS NOT NULL),
    CHECK ((ledger_entries IS NULL) = (ledger_digest IS NULL)),
    CHECK (scored_seq IS NULL OR ledger_entries IS NOT NULL),
    CHECK (assessed_at <= recorded_at)
);

-- Q7: a plan's one-time holdout read is scored at most once, by one receipt.
CREATE UNIQUE INDEX evaluation_receipt_one_scored_read
    ON app.evaluation_receipt (tenant_id, plan_id) WHERE scored_seq IS NOT NULL;

CREATE FUNCTION app.assessor_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    p app.assessor_protocol%ROWTYPE;
    a app.holdout_access%ROWTYPE;
    ok boolean;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock', TG_TABLE_NAME,
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'assessor_protocol' THEN
        RETURN NEW;
    ELSIF TG_TABLE_NAME = 'holdout_access' THEN
        ok := NEW.seq = 1 + coalesce((SELECT max(seq) FROM app.holdout_access
                WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id), 0)
            AND EXISTS (SELECT 1 FROM app.assessor_protocol
                WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id);
    ELSE
        SELECT * INTO p FROM app.assessor_protocol
            WHERE tenant_id = NEW.tenant_id AND experiment_id = NEW.experiment_id;
        IF TG_TABLE_NAME = 'assessor_candidate' THEN
            ok := NEW.depth = (SELECT count(*) FROM app.assessor_candidate
                    WHERE tenant_id = NEW.tenant_id
                      AND experiment_id = NEW.experiment_id)
                AND NEW.parent_id = CASE WHEN NEW.depth = 0 THEN p.baseline_version
                    ELSE (SELECT candidate_id FROM app.assessor_candidate
                          WHERE tenant_id = NEW.tenant_id
                            AND experiment_id = NEW.experiment_id
                            AND depth = NEW.depth - 1) END;
        ELSE
            SELECT * INTO a FROM app.holdout_access
                WHERE tenant_id = NEW.tenant_id AND plan_id = NEW.plan_id
                  AND seq = NEW.scored_seq;
            ok := (NEW.protocol_hash, NEW.plan_id) = (p.protocol_hash, p.plan_id)
                AND NEW.candidate_hash = (SELECT content_hash
                    FROM app.assessor_candidate WHERE tenant_id = NEW.tenant_id
                      AND experiment_id = NEW.experiment_id
                      AND candidate_id = NEW.candidate_id)
                AND ((NEW.assessor_actor_id, NEW.assessor_route_id,
                      NEW.assessor_family_id, NEW.assessor_version)
                     = (p.assessor_actor_id, p.assessor_route_id,
                        p.assessor_family_id, p.assessor_version)
                     OR (NEW.decision = 'blocked'
                         AND 'assessor_not_registered' = ANY (NEW.reasons)))
                AND (NEW.scored_seq IS NULL OR (a.actor_id, a.trial_id, a.family)
                     = (NEW.assessor_actor_id, NEW.trial_ids[1], NEW.experiment_id));
        END IF;
    END IF;
    IF ok IS NOT TRUE THEN
        RAISE EXCEPTION 'app.%: not consistent with the stored protocol',
            TG_TABLE_NAME USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['assessor_protocol', 'assessor_candidate',
                             'holdout_access', 'evaluation_receipt'] LOOP
        EXECUTE format('CREATE TRIGGER insert_guard BEFORE INSERT ON app.%I '
            'FOR EACH ROW EXECUTE FUNCTION app.assessor_guard()', t);
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR '
            'TRUNCATE ON app.%I FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.journal_append_only()', t);
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY, '
            'FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON app.%I '
            'USING (tenant_id = app.current_tenant_id())', t);
        EXECUTE format('GRANT SELECT ON app.%I TO qw_worker', t);
    END LOOP;
END
$$;

GRANT INSERT (tenant_id, experiment_id, protocol_hash, plan_id, baseline_version,
    assessor_actor_id, assessor_route_id, assessor_family_id, assessor_version,
    body, recorded_at, event_hash) ON app.assessor_protocol TO qw_worker;
GRANT INSERT (tenant_id, experiment_id, candidate_id, depth, parent_id,
    content_hash, body, recorded_at, event_hash)
    ON app.assessor_candidate TO qw_worker;
GRANT INSERT (tenant_id, plan_id, seq, access_id, trial_id, family, actor_id, body,
    recorded_at, event_hash) ON app.holdout_access TO qw_worker;
GRANT INSERT (tenant_id, experiment_id, verdict_id, candidate_id, candidate_hash,
    protocol_hash, assessor_actor_id, assessor_route_id, assessor_family_id,
    assessor_version, decision, reasons, scores, trial_ids, failed_trial_ids,
    limitations, assessed_at, plan_id, scored_seq, ledger_entries, ledger_digest,
    recorded_at, event_hash) ON app.evaluation_receipt TO qw_worker;
-- Q3 and Read A: tenants may later see the verdict, never holdout data or values.
GRANT SELECT (tenant_id, experiment_id, verdict_id, candidate_id, candidate_hash,
    protocol_hash, assessor_version, decision, reasons, trial_ids, failed_trial_ids,
    limitations, assessed_at, recorded_at) ON app.evaluation_receipt TO qw_app;

ALTER TABLE app.experiment_revision
    ADD CONSTRAINT experiment_revision_protocol_0028
        FOREIGN KEY (tenant_id, experiment_id, protocol_id)
        REFERENCES app.assessor_protocol (tenant_id, experiment_id, protocol_hash)
        NOT VALID,
    ADD CONSTRAINT experiment_revision_receipt_0028
        FOREIGN KEY (tenant_id, experiment_id, verdict_id, decision)
        REFERENCES app.evaluation_receipt (tenant_id, experiment_id, verdict_id,
            decision) NOT VALID;
