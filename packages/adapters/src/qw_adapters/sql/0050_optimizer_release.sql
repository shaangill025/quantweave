-- 0050: optimizer releases (T050 increment 7; R076, R092, R099; owner decisions 23,
-- 33, 88, 95 and 96). 0029 refused every code or optimizer release row (R076). A
-- recursive-optimizer release may now be recorded, so it can be prepared, signed
-- and reviewed before T007 qualifies the sandbox; application code stays refused.
-- - Writer: the operator login (qw_release, owner decision 95, "qw_release writes
--   them"), never qw_worker: the optimizer runs as qw_worker and must never write
--   its own release or the maintainer approval in it (R092). qw_release reads only
--   optimizer rows (`record_release`'s existence check by id; a clash with another
--   target's id is then a refused insert, never a read of that row). qw_worker
--   keeps its 0029 write for every other target and its read of all rows
--   (install and replay read them).
--   qw_assessor and qw_app hold nothing, as before.
-- - Install stays refused until QUAL17 (T007): no deployment event may name an
--   optimizer release as its release or rollback target (`sandbox_unqualified`).
--   The guard runs as the inserting role and fails closed: a release row that
--   role cannot read is refused too. The domain refuses the same install
--   (`qw_domain.release._target_gates`); opening it is a later, reviewed migration.
-- - Not opened here (designed in docs/receipts/T050.md, increment 7): storing the
--   domain's rollback to the frozen baseline (decisions 88, 96; 0029 requires a
--   rollback target release) and admitting an installed V1 in 0047's cycle guard.

ALTER TABLE app.signed_release DROP CONSTRAINT signed_release_target_check,
    ADD CONSTRAINT signed_release_target_check CHECK (target IN
        ('personal_adaptation', 'research', 'strategy', 'evaluator',
         'recursive_optimizer'));

DROP POLICY installation_worker ON app.signed_release;
CREATE POLICY installation_worker ON app.signed_release TO qw_worker
    USING (true) WITH CHECK (target <> 'recursive_optimizer');
CREATE POLICY optimizer_release_operator ON app.signed_release TO qw_release
    USING (target = 'recursive_optimizer') WITH CHECK (target = 'recursive_optimizer');
GRANT SELECT ON app.signed_release TO qw_release;
GRANT INSERT (release_id, component, target, candidate_id, candidate_hash,
    verdict_id, manifest_hash, approver_id, approved_at, min_schema, max_schema,
    writes_schema, key_id, algorithm, signature, body, recorded_at, event_hash,
    verdict_source) ON app.signed_release TO qw_release;

CREATE FUNCTION app.release_sandbox_gate() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[NEW.release_id, NEW.to_release_id] LOOP
        CONTINUE WHEN t IS NULL;
        IF coalesce((SELECT s.target <> 'recursive_optimizer'
                FROM app.signed_release s WHERE s.release_id = t), false)
            IS NOT TRUE THEN
            RAISE EXCEPTION 'app.deployment_event: sandbox_unqualified (%)', t
                USING ERRCODE = 'check_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END
$$;

CREATE TRIGGER sandbox_gate BEFORE INSERT ON app.deployment_event
    FOR EACH ROW EXECUTE FUNCTION app.release_sandbox_gate();
