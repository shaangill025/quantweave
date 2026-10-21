-- Experiment lineage served (project contract 0.11.0; follow-up of owner decision
-- 19, 2026-10-21). The number is provisional: the runner needs contiguous numbers,
-- so this branch develops it as the next free one; it is renumbered at commit.
-- `retest_of` lives in the frozen assessor protocol, which qw_app may not read at
-- all (owner decision 17), so no grant on an assessor table is added. Instead each
-- experiment revision carries a copy, hashed with the revision once set (absent
-- from the hash when null, so stored revisions verify as before), and immutable
-- between revisions (the domain's `may_follow`). qw_worker writes it in phase 2
-- from the record that phase 1 derived from the stored protocol; this guard holds
-- it equal to that protocol's link, null included. The guard runs as the owner
-- (qw_migrate, SECURITY DEFINER, fixed search_path, no EXECUTE grant) because
-- qw_worker cannot read assessor_protocol; forced RLS still scopes it to the
-- session tenant, and its error names no protected value. For qw_worker it is an
-- own-tenant oracle only: a refusal says the row's link is not its own tenant's
-- protocol's, which qw_worker already learns from its SELECT on
-- experiment_revision (earlier revisions carry the copy); another tenant's
-- protocol is invisible to it under RLS, so nothing crosses tenants.

ALTER TABLE app.experiment_revision
    ADD COLUMN retest_of text,
    ADD CONSTRAINT experiment_revision_retest_of_0034
        CHECK (retest_of ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
            AND retest_of <> experiment_id);

-- Fail closed: a revision stored before this migration has no copy, so one whose
-- protocol links another experiment would be served without its link (and could
-- never advance: the next revision's copy could not equal the stored one). The
-- runner applies migrations as qw_migrate, NOBYPASSRLS under forced RLS with no
-- tenant set, so without help this check would see no rows and always pass. Two
-- SELECT-only policies for qw_migrate let it see every tenant's rows; they are
-- created and dropped inside this migration's own transaction (a refusal rolls
-- them back with it), so no role ever holds them after the migration. As in
-- 0005 and 0006, the policies are TO qw_migrate only, which already owns the
-- tables.
CREATE POLICY retest_check ON app.experiment_revision
    FOR SELECT TO qw_migrate USING (true);
CREATE POLICY retest_check ON app.assessor_protocol
    FOR SELECT TO qw_migrate USING (true);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM app.experiment_revision r
        JOIN app.assessor_protocol p ON (p.tenant_id, p.experiment_id,
            p.protocol_hash) = (r.tenant_id, r.experiment_id, r.protocol_id)
        WHERE r.retest_of IS DISTINCT FROM p.retest_of
    ) THEN
        RAISE EXCEPTION 'experiment_revision: a stored retest_of copy differs from '
            'its protocol''s; re-hash those revisions in a reviewed migration'
            USING ERRCODE = 'check_violation';
    END IF;
END
$$;
DROP POLICY retest_check ON app.experiment_revision;
DROP POLICY retest_check ON app.assessor_protocol;

CREATE FUNCTION app.experiment_retest_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.retest_of IS DISTINCT FROM (
        SELECT p.retest_of FROM app.assessor_protocol p
        WHERE p.tenant_id = NEW.tenant_id AND p.experiment_id = NEW.experiment_id
          AND p.protocol_hash = NEW.protocol_id
    ) THEN
        RAISE EXCEPTION 'app.experiment_revision: retest_of is not the protocol''s'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app.experiment_retest_guard() FROM PUBLIC;

-- After insert_guard (triggers fire in name order), so the 0026 chain rules first.
CREATE TRIGGER retest_guard BEFORE INSERT ON app.experiment_revision
    FOR EACH ROW EXECUTE FUNCTION app.experiment_retest_guard();

GRANT INSERT (retest_of) ON app.experiment_revision TO qw_worker;
