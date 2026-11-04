-- 0063 (T058): simulations become deletable by the retention.delete job (spec §12;
-- data dictionary simulation_account / simulation_order_fill, class
-- `simulation`, deleted on request under owner decision 20's defaults). Must be
-- applied after 0062 (numbers are contiguous; 0062 lands separately).
-- - Only a qw_worker member deletes (`qw_adapters.deletion_targets`, run by the
--   job after it has read the legal holds). qw_app, qw_assessor and qw_release
--   get no DELETE grant. The 0014 statement trigger keeps refusing UPDATE and
--   TRUNCATE to everyone; a DELETE is refused, with the same message, to every
--   session that is not a qw_worker member or is a superuser (the owner too).
-- - Whole simulations only: at commit, a deleted entry, order, fill or digest
--   row's simulation must be gone as well (its definition can go only after its
--   rows, by 0014's foreign keys), so no partial history is ever left behind.
-- - Every statement runs under 0014's forced RLS: a tenant's rows only.
-- - Accepted residuals: legal holds are enforced by the job (it reads them
--   before deleting), not here; any qw_worker session may delete a whole
--   simulation. And a sweep with candidates holds the store's exclusive tenant
--   lock (`simulation:<tenant>`) for its transaction, blocking that tenant's
--   simulation writers meanwhile.
-- - 0014 (applied, checksummed) is not edited: its header still says
--   append-only; this file is the pointer.

CREATE FUNCTION app.simulation_delete_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF TG_OP <> 'DELETE'
       OR NOT pg_catalog.pg_has_role(current_user, 'qw_worker', 'MEMBER')
       OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r
                    WHERE r.rolname = current_user), true) THEN
        RAISE EXCEPTION 'app.% is append-only: % refused', TG_TABLE_NAME, TG_OP;
    END IF;
    RETURN NULL;
END
$$;

CREATE FUNCTION app.simulation_deleted_whole() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM app.simulation s WHERE s.tenant_id = OLD.tenant_id
               AND s.id = OLD.simulation_id) THEN
        RAISE EXCEPTION 'app.%: delete a whole simulation, not part of %',
            TG_TABLE_NAME, OLD.simulation_id;
    END IF;
    RETURN NULL;
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['simulation', 'simulation_entry', 'simulation_order',
                             'simulation_fill', 'simulation_digest'] LOOP
        EXECUTE format('DROP TRIGGER append_only ON app.%I', t);
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR TRUNCATE ON '
            'app.%I FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only()', t);
        EXECUTE format('CREATE TRIGGER delete_guard BEFORE DELETE ON app.%I FOR '
            'EACH STATEMENT EXECUTE FUNCTION app.simulation_delete_guard()', t);
        IF t <> 'simulation' THEN
            EXECUTE format('CREATE CONSTRAINT TRIGGER deleted_whole AFTER DELETE ON '
                'app.%I DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION '
                'app.simulation_deleted_whole()', t);
        END IF;
        EXECUTE format('GRANT DELETE ON app.%I TO qw_worker', t);
    END LOOP;
END
$$;
