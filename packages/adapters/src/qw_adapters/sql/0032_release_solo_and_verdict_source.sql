-- T051 owner decisions 23 and 33 on the 0029 release ledger (qw_adapters
-- release_store). Rows recorded before this migration keep NULL in the new columns
-- and their hashes (the store reads NULL as "no opt-in", "not self-approved" and
-- "caller supplied", true of every earlier row: 0029 refused signer installs and
-- no other verdict source existed); NOT VALID CHECKs bind every new row.
-- - Decision 23: `release_deployment.solo_maintainer` is the explicit opt-in, only
--   on a self-hosted deployment with exactly one operator. An install is
--   `self_approved` exactly when its operator is the release's signer, which is
--   allowed only on an opted-in deployment; rollbacks and pauses never are.
-- - Decision 33: `signed_release.verdict_source` is 'caller_supplied' (the T044
--   verdict re-checked against the caller's registry) until an installation-scope
--   assessor store exists; that store adds its own value and replaces the mark.
-- The marks are hashed by the store. The guard below is 0029's with the
-- separation rule replaced by the self-approval rule.

ALTER TABLE app.release_deployment ADD COLUMN solo_maintainer boolean,
    ADD CONSTRAINT release_deployment_solo_maintainer CHECK (
        solo_maintainer IS NOT NULL AND (NOT solo_maintainer
        OR (edition = 'self_hosted' AND cardinality(operators) = 1))) NOT VALID;
ALTER TABLE app.signed_release ADD COLUMN verdict_source text,
    ADD CONSTRAINT signed_release_verdict_source
        CHECK (verdict_source IS NOT NULL
        AND verdict_source = 'caller_supplied') NOT VALID;
ALTER TABLE app.deployment_event ADD COLUMN self_approved boolean,
    ADD CONSTRAINT deployment_event_self_approved CHECK (
        self_approved IS NOT NULL AND (kind = 'install' OR NOT self_approved))
        NOT VALID;
GRANT INSERT (solo_maintainer) ON app.release_deployment TO qw_worker;
GRANT INSERT (verdict_source) ON app.signed_release TO qw_worker;
GRANT INSERT (self_approved) ON app.deployment_event TO qw_worker;

CREATE OR REPLACE FUNCTION app.release_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    d app.release_deployment%ROWTYPE;
    r app.signed_release%ROWTYPE;
    p app.deployment_event%ROWTYPE;
    ts timestamptz;
    before integer;
    head text;
    ok boolean;
BEGIN
    IF TG_TABLE_NAME = 'deployment_event' THEN
        ts := NEW.at;
    ELSE
        ts := NEW.recorded_at;
    END IF;
    IF ts > clock_timestamp() OR ts < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock', TG_TABLE_NAME, ts
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'release_deployment' THEN  -- the store's sorted sets
        IF NEW.operators IS DISTINCT FROM ARRAY(SELECT DISTINCT x COLLATE "C"
               FROM unnest(NEW.operators) AS u (x) ORDER BY 1)
           OR NEW.tenants IS DISTINCT FROM ARRAY(SELECT DISTINCT x COLLATE "C"
               FROM unnest(NEW.tenants) AS u (x) ORDER BY 1) THEN
            RAISE EXCEPTION 'app.release_deployment: members are not a sorted set'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_TABLE_NAME <> 'deployment_event' THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'release:' || NEW.deployment_id, 0));
    SELECT * INTO d FROM app.release_deployment
        WHERE deployment_id = NEW.deployment_id;
    SELECT * INTO p FROM app.deployment_event WHERE deployment_id = NEW.deployment_id
        ORDER BY seq DESC LIMIT 1;
    before := coalesce(p.schema_after, d.initial_schema);
    SELECT CASE kind WHEN 'rollback' THEN to_release_id ELSE release_id END
        INTO head FROM app.deployment_event
        WHERE deployment_id = NEW.deployment_id AND component = NEW.component
        ORDER BY seq DESC LIMIT 1;
    ok := NEW.seq = coalesce(p.seq, 0) + 1 AND NEW.at >= coalesce(p.at, NEW.at)
        AND NEW.operator_id = ANY (d.operators)
        AND NOT EXISTS (SELECT 1 FROM app.deployment_event e  -- a signer's step-up
            JOIN app.signed_release s ON s.release_id = e.release_id
            WHERE e.deployment_id = NEW.deployment_id AND e.kind = 'install'
              AND s.body -> 'approval' ->> 1 = NEW.step_up_receipt_id);
    IF NEW.kind = 'install' THEN
        SELECT * INTO r FROM app.signed_release WHERE release_id = NEW.release_id;
        ok := ok AND r.component = NEW.component AND NEW.at >= r.approved_at
            AND NEW.self_approved = (NEW.operator_id = r.approver_id)
            AND (NOT NEW.self_approved OR d.solo_maintainer IS TRUE)  -- decision 23
            AND before BETWEEN r.min_schema AND r.max_schema
            AND NEW.schema_after = greatest(before, r.writes_schema)
            AND r.body -> 'approval' ->> 1 <> NEW.step_up_receipt_id
            AND NOT EXISTS (SELECT 1 FROM app.deployment_event e
                WHERE e.deployment_id = NEW.deployment_id
                  AND e.step_up_receipt_id = r.body -> 'approval' ->> 1)
            AND NOT EXISTS (SELECT 1 FROM app.deployment_event e
                JOIN app.signed_release s ON s.release_id = e.release_id
                WHERE e.deployment_id = NEW.deployment_id AND e.kind = 'install'
                  AND s.release_id <> r.release_id  -- a re-install: the index
                  AND s.body -> 'approval' ->> 1 = r.body -> 'approval' ->> 1);
    ELSE
        ok := ok AND NEW.release_id = head AND NEW.schema_after = before;
        IF NEW.kind = 'rollback' THEN
            SELECT * INTO r FROM app.signed_release WHERE release_id = NEW.to_release_id;
            ok := ok AND NEW.to_release_id <> NEW.release_id
                AND r.component = NEW.component
                AND before BETWEEN r.min_schema AND r.max_schema
                AND EXISTS (SELECT 1 FROM app.deployment_event
                    WHERE deployment_id = NEW.deployment_id AND kind = 'install'
                      AND release_id = NEW.to_release_id)
                AND NOT EXISTS (SELECT 1 FROM app.deployment_event
                    WHERE deployment_id = NEW.deployment_id
                      AND kind IN ('rollback', 'pause')
                      AND release_id = NEW.to_release_id);  -- withdrawn or paused
        END IF;
    END IF;
    IF ok IS NOT TRUE THEN
        RAISE EXCEPTION 'app.deployment_event: not the next event of %',
            NEW.deployment_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
