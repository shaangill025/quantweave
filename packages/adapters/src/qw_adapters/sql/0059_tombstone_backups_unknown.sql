-- Owner decision 114 (T058 PR B): until a backup catalog exists (T063), a deletion
-- cannot list the backups that still hold a copy, so each tombstone the deletion
-- job writes carries an explicit `residual_backups_unknown` marker instead of an
-- empty list, which would read as "no backup holds this". Restore still
-- suppresses the record through the tombstone. The marker is a release gate
-- until T063.
-- - The column is added, not edited into 0057: earlier rows keep false, and
--   their entry hashes keep the `qw-tombstone-2` bytes. A marked row hashes as
--   `qw-tombstone-3`: the same list with `true` appended
--   (qw_domain.retention.entry_hash). The guard below is 0057's with only that
--   recompute changed.
-- - A marked row lists no backup and no expiry (CHECK).
-- - qw_worker may insert the column; nobody updates it (0057's append_only).

ALTER TABLE app.deletion_tombstone
    ADD COLUMN residual_backups_unknown boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT deletion_tombstone_backups_unknown CHECK (
        NOT residual_backups_unknown
        OR (cardinality(residual_backups) = 0 AND backup_expiry_at IS NULL));

CREATE OR REPLACE FUNCTION app.deletion_tombstone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    now_ CONSTANT timestamptz := clock_timestamp();
    latest timestamptz;
    p record;
    n bigint;
    sealed text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'deletion_tombstone:' || NEW.tenant_id::text, 0));
    SELECT max(t) INTO latest FROM (
        SELECT max(recorded_at) t FROM app.deletion_tombstone
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.deletion_tombstone_checkpoint
            WHERE tenant_id = NEW.tenant_id) x;
    NEW.recorded_at := greatest(now_, latest);  -- any value given is replaced
    SELECT seq, entry_hash, deleted_at, key_id INTO p FROM app.deletion_tombstone
        WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    IF TG_TABLE_NAME = 'deletion_tombstone' THEN
        IF NEW.seq IS DISTINCT FROM coalesce(p.seq + 1, 0)
           OR NEW.prev_hash IS DISTINCT FROM coalesce(p.entry_hash, repeat('0', 64))
        THEN
            RAISE EXCEPTION 'app.deletion_tombstone: not the next entry'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.key_id IS DISTINCT FROM coalesce(p.key_id, NEW.key_id) THEN
            RAISE EXCEPTION 'app.deletion_tombstone: key_mismatch'
                USING ERRCODE = 'check_violation';  -- one key per chain
        END IF;
        IF NEW.deleted_at < p.deleted_at THEN
            RAISE EXCEPTION 'app.deletion_tombstone: time_order'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.deleted_at > greatest(now_, p.deleted_at)
           OR NEW.deleted_at < now_ - interval '60 seconds' THEN
            RAISE EXCEPTION 'app.deletion_tombstone: stale_clock'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.entry_hash IS DISTINCT FROM encode(sha256(convert_to(
            '["qw-tombstone-'
            || CASE WHEN NEW.residual_backups_unknown THEN '3' ELSE '2' END
            || '",' || NEW.seq || ',"' || NEW.tenant_id || '","'
            || NEW.record_hash || '","' || NEW.cls || '","' || NEW.job_id || '","'
            || app.deletion_tombstone_instant(NEW.requested_at) || '","'
            || app.deletion_tombstone_instant(NEW.deleted_at) || '",['
            || coalesce((SELECT string_agg('"' || bk || '"', ',' ORDER BY ord)
                FROM unnest(NEW.residual_backups) WITH ORDINALITY u (bk, ord)), '')
            || '],' || coalesce('"' || app.deletion_tombstone_instant(
                NEW.backup_expiry_at) || '"', 'null')
            || ',"' || NEW.hash_scheme || '","' || NEW.key_id || '","'
            || NEW.prev_hash || '"'
            || CASE WHEN NEW.residual_backups_unknown THEN ',true' ELSE '' END
            || ']', 'UTF8')), 'hex') THEN
            RAISE EXCEPTION 'app.deletion_tombstone: entry_hash'
                USING ERRCODE = 'check_violation';
        END IF;
        sealed := NEW.entry_hash;
    ELSE
        n := coalesce(p.seq + 1, 0);
        IF NEW.length IS DISTINCT FROM n OR NEW.head IS DISTINCT FROM p.entry_hash
           OR EXISTS (SELECT 1 FROM app.deletion_tombstone_checkpoint c
                      WHERE c.tenant_id = NEW.tenant_id AND c.length >= n) THEN
            RAISE EXCEPTION 'app.deletion_tombstone_checkpoint: not the head'
                USING ERRCODE = 'check_violation';
        END IF;
        sealed := NEW.head;
    END IF;
    NEW.row_hash := encode(sha256(convert_to(sealed || '|'
        || app.deletion_tombstone_instant(NEW.recorded_at), 'UTF8')), 'hex');
    RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION app.deletion_tombstone_guard() FROM PUBLIC;

GRANT INSERT (residual_backups_unknown) ON app.deletion_tombstone TO qw_worker;
