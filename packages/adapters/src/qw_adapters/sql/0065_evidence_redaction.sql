-- 0065 (T058): evidence deletion also removes the passage readings (owner decision
-- 124; spec §12; data dictionary evidence_record, class `evidence`). Must be
-- applied after 0064 (numbers are contiguous; 0064 lands separately).
-- - `evidence_record.reading` becomes nullable: NULL is a passage redacted with
--   its deleted source. Ids, offsets, hashes, citations (replay-limited) and the
--   source rows (metadata) stay; the text already goes under 0012.
-- - The one UPDATE: reading to NULL, every other column unchanged, of a passage
--   whose source has a `deleted` event, by a qw_worker member that is not a
--   superuser (the retention.delete job's evidence target). Column grant to
--   qw_worker only; the owner and superusers get the "append-only" refusal.
--   DELETE and TRUNCATE stay refused to everyone (0012's statement trigger).
-- - At commit, a passage holds a reading exactly when its source is not
--   deleted: a tombstone cannot commit while a passage of its source keeps a
--   reading, and a NULL reading cannot commit without its source's tombstone.
-- - Existing passages of deleted sources are redacted here (before the guard).
-- - 0012 (applied, checksummed) is not edited; this file is the pointer.
-- - Not changed (owner question): the source rows keep their NOT NULL metadata
--   (feed, publication, receipt and retention times, scope, jurisdiction), and
--   claims keep their stated values (research class, decision 121).

DROP TRIGGER append_only ON app.evidence_record;
ALTER TABLE app.evidence_record ALTER COLUMN reading DROP NOT NULL;
-- The backfill runs as the runner's qw_migrate: NOBYPASSRLS under forced RLS with
-- no tenant set, so (as 0034) temporary policies TO qw_migrate only let it see
-- and redact every tenant's rows; they are dropped before the end. It runs
-- before the UPDATE guard and the commit checks exist, and fails the migration
-- if any deleted source's passage still holds a reading.
-- backfill begin
CREATE POLICY redaction_backfill ON app.evidence_event
    FOR SELECT TO qw_migrate USING (true);
CREATE POLICY redaction_backfill ON app.evidence_record
    FOR SELECT TO qw_migrate USING (true);
CREATE POLICY redaction_backfill_update ON app.evidence_record
    FOR UPDATE TO qw_migrate USING (true) WITH CHECK (true);
UPDATE app.evidence_record r SET reading = NULL WHERE EXISTS (
    SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = r.tenant_id
    AND e.kind = 'deleted' AND e.subject = r.source_id);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM app.evidence_record r JOIN app.evidence_event e
               ON e.tenant_id = r.tenant_id AND e.kind = 'deleted'
               AND e.subject = r.source_id WHERE r.reading IS NOT NULL) THEN
        RAISE EXCEPTION 'evidence_record: a deleted source keeps a passage reading '
            'after the backfill' USING ERRCODE = 'check_violation';
    END IF;
END
$$;
DROP POLICY redaction_backfill_update ON app.evidence_record;
DROP POLICY redaction_backfill ON app.evidence_record;
DROP POLICY redaction_backfill ON app.evidence_event;
-- backfill end
CREATE TRIGGER append_only BEFORE DELETE OR TRUNCATE ON app.evidence_record
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

CREATE FUNCTION app.evidence_redact_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF NOT pg_catalog.pg_has_role(current_user, 'qw_worker', 'MEMBER')
       OR coalesce((SELECT r.rolsuper FROM pg_catalog.pg_roles r
                    WHERE r.rolname = current_user), true)
       OR NEW.reading IS NOT NULL
       OR (NEW.tenant_id, NEW.id, NEW.source_id, NEW.start_offset, NEW.end_offset,
           NEW.hash, NEW.known_at) IS DISTINCT FROM (OLD.tenant_id, OLD.id,
           OLD.source_id, OLD.start_offset, OLD.end_offset, OLD.hash, OLD.known_at)
       OR NOT EXISTS (SELECT 1 FROM app.evidence_event e
                      WHERE e.tenant_id = OLD.tenant_id AND e.kind = 'deleted'
                      AND e.subject = OLD.source_id) THEN
        RAISE EXCEPTION 'app.evidence_record is append-only: UPDATE refused';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION app.evidence_redacted() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    row jsonb := to_jsonb(NEW);
    sid text := coalesce(row ->> 'source_id', row ->> 'subject');
    gone boolean;
BEGIN
    IF TG_TABLE_NAME = 'evidence_event' AND (row ->> 'kind') <> 'deleted' THEN
        RETURN NULL;
    END IF;
    gone := EXISTS (SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = NEW.tenant_id
                    AND e.kind = 'deleted' AND e.subject = sid);
    IF gone AND EXISTS (SELECT 1 FROM app.evidence_record r
                        WHERE r.tenant_id = NEW.tenant_id AND r.source_id = sid
                        AND r.reading IS NOT NULL) THEN
        RAISE EXCEPTION 'app.evidence: deleted source % keeps a passage reading', sid
            USING ERRCODE = 'insufficient_privilege';
    ELSIF NOT gone AND TG_TABLE_NAME = 'evidence_record'
          AND jsonb_typeof(row -> 'reading') = 'null' THEN
        RAISE EXCEPTION 'app.evidence_record: % has no reading but % is not deleted',
            row ->> 'id', sid USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER redact_guard BEFORE UPDATE ON app.evidence_record
    FOR EACH ROW EXECUTE FUNCTION app.evidence_redact_guard();
-- Named to fire after 0012's `text_gone` (constraint triggers run in name order).
CREATE CONSTRAINT TRIGGER unredacted AFTER INSERT OR UPDATE ON app.evidence_record
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.evidence_redacted();
CREATE CONSTRAINT TRIGGER unredacted AFTER INSERT ON app.evidence_event
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.evidence_redacted();

GRANT UPDATE (reading) ON app.evidence_record TO qw_worker;
