-- Owner decisions 35 to 37 (2026-10-21): a strategy promotion clears the excess
-- lower bound recomputed at the Bonferroni-widened z' (k = chain_trials). The
-- receipt keeps the assessor's adjustment: `chain_trials`, `z_widened` and
-- `baseline.<id>.excess_lower_widened` per tested baseline, as decimal strings
-- (protected: qw_app gets no privilege). The database holds what it can check
-- without the inverse normal: a strategy acceptance or rejection names the
-- receipt's chain count, z' and at least one bound, an acceptance has no
-- negative bound, and other targets carry no adjustment. Older rows: no change.

ALTER TABLE app.evaluation_receipt
    ADD COLUMN adjustment jsonb
        CHECK (adjustment IS NULL OR jsonb_typeof(adjustment) = 'object');

CREATE FUNCTION app.adjustment_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    strategy boolean;
    bounds numeric[];
BEGIN
    SELECT body ->> 'target' = 'strategy' INTO strategy FROM app.assessor_protocol
        WHERE tenant_id = NEW.tenant_id AND experiment_id = NEW.experiment_id;
    IF NEW.adjustment IS NOT NULL THEN
        SELECT array_agg(value::numeric) INTO bounds
            FROM jsonb_each_text(NEW.adjustment)
            WHERE key LIKE 'baseline.%.excess_lower_widened';
    END IF;
    IF (strategy IS NOT TRUE AND NEW.adjustment IS NOT NULL) OR (
        strategy AND NEW.decision IN ('accept_for_review', 'reject') AND (
            -- a missing adjustment fails here too (NULL is distinct from k)
            (NEW.adjustment ->> 'chain_trials')::numeric
                IS DISTINCT FROM NEW.chain_trials
            OR NOT NEW.adjustment ? 'z_widened'
            OR coalesce(cardinality(bounds), 0) = 0
            OR (NEW.decision = 'accept_for_review' AND 0 > ANY (bounds))))
    THEN
        RAISE EXCEPTION 'app.evaluation_receipt: inconsistent adjustment'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- Named to fire first (triggers fire in name order); the 0028 rules follow.
CREATE TRIGGER adjustment_guard BEFORE INSERT ON app.evaluation_receipt
    FOR EACH ROW EXECUTE FUNCTION app.adjustment_guard();

GRANT INSERT (adjustment) ON app.evaluation_receipt TO qw_assessor;
