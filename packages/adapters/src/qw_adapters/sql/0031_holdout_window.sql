-- 0031: owner decision 18 (2026-10-21), with decision 7 of 2026-10-20 ("the
-- protected holdout is never re-used"). Once a promotion window's holdout read is
-- scored (a receipt names it), no other plan in the same scope may read or score
-- an overlapping window: `holdout_window_used`. Scope (owner choice, 2026-10-21,
-- "Tenant + target"): the tenant and the protocol's improvement target, whatever
-- the experiment or baseline; a plan's scope is that of the stored protocols
-- naming its plan_id. An empty window is refused.
-- Windows are the stored reads' half-open [start, end) (Access wire item 5).
-- Checked before inserting a read or a scored receipt, under the 0026 lock
-- 'experiment:<tenant>'; same-plan reuse stays with 0028 (one scored receipt per
-- plan). Unscored reads do not use a window.

CREATE FUNCTION app.holdout_window_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    w tstzrange;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    IF TG_TABLE_NAME = 'holdout_access' THEN
        w := tstzrange((NEW.body -> 5 ->> 0)::timestamptz,
                       (NEW.body -> 5 ->> 1)::timestamptz, '[)');
    ELSIF NEW.scored_seq IS NULL THEN
        RETURN NEW;
    ELSE
        SELECT tstzrange((h.body -> 5 ->> 0)::timestamptz,
                         (h.body -> 5 ->> 1)::timestamptz, '[)') INTO w
            FROM app.holdout_access h
            WHERE h.tenant_id = NEW.tenant_id AND h.plan_id = NEW.plan_id
              AND h.seq = NEW.scored_seq;
    END IF;
    IF w IS NULL OR isempty(w) THEN
        RAISE EXCEPTION 'app.%: an empty or missing holdout window', TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
        SELECT 1 FROM app.evaluation_receipt r
        JOIN app.holdout_access h ON (h.tenant_id, h.plan_id, h.seq)
            = (r.tenant_id, r.plan_id, r.scored_seq)
        JOIN app.assessor_protocol used ON (used.tenant_id, used.experiment_id)
            = (r.tenant_id, r.experiment_id)
        JOIN app.assessor_protocol mine ON mine.tenant_id = NEW.tenant_id
            AND mine.plan_id = NEW.plan_id
        WHERE r.tenant_id = NEW.tenant_id AND r.plan_id <> NEW.plan_id
          AND used.body ->> 'target' = mine.body ->> 'target'
          AND tstzrange((h.body -> 5 ->> 0)::timestamptz,
                        (h.body -> 5 ->> 1)::timestamptz, '[)') && w
    ) THEN
        RAISE EXCEPTION 'app.%: holdout_window_used', TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- After insert_guard (triggers fire in name order), so the 0028 rules come first.
CREATE TRIGGER window_guard BEFORE INSERT ON app.holdout_access
    FOR EACH ROW EXECUTE FUNCTION app.holdout_window_guard();
CREATE TRIGGER window_guard BEFORE INSERT ON app.evaluation_receipt
    FOR EACH ROW EXECUTE FUNCTION app.holdout_window_guard();

-- Owner decision 19 (2026-10-21): an optional `retest_of` link from an experiment's
-- frozen protocol to the experiment it re-tests (same tenant: the composite key;
-- it must already be stored; never itself). Rows are append-only and the link is
-- in the hashed body, so it is immutable, and since its target exists first no
-- cycle can form. Each new receipt counts its family's trials (the ledger's
-- trial_count) and the whole chain's (`chain_trials`, the count a multiple-
-- testing adjustment must use; none is applied). The database holds a lower
-- bound: the larger of this receipt's and the experiment's stored count, plus
-- each ancestor's largest stored count. 0028 rows carry no counts (NOT VALID).

ALTER TABLE app.assessor_protocol
    ADD COLUMN retest_of text,
    ADD CONSTRAINT assessor_protocol_retest_of_0031
        FOREIGN KEY (tenant_id, retest_of)
        REFERENCES app.assessor_protocol (tenant_id, experiment_id),
    ADD CONSTRAINT assessor_protocol_retest_body_0031
        CHECK (retest_of IS DISTINCT FROM experiment_id
            AND body ->> 'retest_of' IS NOT DISTINCT FROM retest_of);

ALTER TABLE app.evaluation_receipt
    ADD COLUMN family_trials integer CHECK (family_trials >= 0),
    ADD COLUMN chain_trials integer,
    ADD CONSTRAINT evaluation_receipt_chain_0031
        CHECK (chain_trials >= family_trials),
    ADD CONSTRAINT evaluation_receipt_counted_0031
        CHECK (family_trials IS NOT NULL AND chain_trials IS NOT NULL) NOT VALID;

CREATE FUNCTION app.retest_chain_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    own integer;
    above integer;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'experiment:' || NEW.tenant_id::text, 0));
    SELECT max(family_trials) INTO own FROM app.evaluation_receipt
        WHERE tenant_id = NEW.tenant_id AND experiment_id = NEW.experiment_id;
    WITH RECURSIVE chain (eid, depth) AS (
        SELECT retest_of, 1 FROM app.assessor_protocol
            WHERE tenant_id = NEW.tenant_id AND experiment_id = NEW.experiment_id
              AND retest_of IS NOT NULL
        UNION ALL
        SELECT p.retest_of, c.depth + 1 FROM chain c
            JOIN app.assessor_protocol p
              ON p.tenant_id = NEW.tenant_id AND p.experiment_id = c.eid
            WHERE p.retest_of IS NOT NULL AND c.depth < 10000
    )
    SELECT coalesce(sum(m.n), 0) INTO above FROM chain
        CROSS JOIN LATERAL (SELECT max(r.family_trials) AS n
            FROM app.evaluation_receipt r WHERE r.tenant_id = NEW.tenant_id
              AND r.experiment_id = chain.eid) m;
    IF NEW.chain_trials < greatest(NEW.family_trials, own) + above THEN
        RAISE EXCEPTION 'app.evaluation_receipt: chain_trials % is below the chain',
            NEW.chain_trials USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER chain_guard BEFORE INSERT ON app.evaluation_receipt
    FOR EACH ROW EXECUTE FUNCTION app.retest_chain_guard();

GRANT INSERT (retest_of) ON app.assessor_protocol TO qw_assessor;
GRANT INSERT (family_trials, chain_trials) ON app.evaluation_receipt TO qw_assessor;
