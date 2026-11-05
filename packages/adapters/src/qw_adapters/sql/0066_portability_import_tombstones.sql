-- 0066: the destination import of the deletion tombstones (T057 increment 10; owner
-- decisions 104, 112 and 114; as 0060, 0062 and 0064). Decision 112 authorises
-- SECURITY DEFINER for these import functions only; 0057's and 0059's guard stays
-- invoker rights. Extends 0057 and 0059, which are not edited.
-- - app.portability_import_tombstones(section): owner qw_migrate, search_path
--   pinned (pg_temp last; pg_catalog's relations named), EXECUTE for qw_app only;
--   the tenant is the transaction's app.current_tenant_id(), never an argument, and
--   forced RLS still binds the definer. Refusals, in order: no tenant (QI003);
--   unless this backend holds `import:<tenant>` exclusive (QI002, 0062's
--   app.portability_lock_held); unless the tenant has no deletion_tombstone and no
--   deletion_tombstone_checkpoint row (QI001, under that lock); then the whole
--   section's shape (QI003) before any insert: qw_domain.tombstone_wire's
--   `{ledger, tombstones, checkpoints}` with exactly its keys at every level, each
--   row its tenant's, JSON types (integers matched as digits, so `true` or `1.5`
--   is no integer), the 0057 and 0059 column CHECKs (app.portability_tombstone_
--   row), seq the row's position and prev_hash the previous entry hash, deletion
--   not after the row's recorded_at, checkpoint lengths strictly increasing with
--   the latest covering the ledger, and every recorded_at no more than 5 minutes
--   past clock_timestamp() (0062's bound; the requested and deletion times are
--   bounded through it). A backup expiry is a future time by nature and is not
--   bounded. jsonb holds no NUL; ids are ID_PATTERN and hashes hex, so no string
--   can carry one. Values are read, never built into SQL.
-- - Rows go in chain order, each checkpoint right after the entry at its length
--   (as tombstone_store.append writes them), with the archive's recorded_at
--   (decision 112). The replaced guard below re-checks each row as for any writer
--   (the chain, one key per chain, deletion order, the entry hash recomputed, each
--   checkpoint the head) and seals row_hash from the kept stamp; the function then
--   requires that seal to be the archive's, and the header recomputed from the
--   stored rows to be the archive's `ledger` (QI003). Decision 114's
--   residual_backups_unknown marker is carried as given; it is inside the entry
--   hash (qw-tombstone-3), so it cannot change in transit unnoticed.
-- - 0059's guard is replaced: every insert first shares `import:<tenant>`, then
--   takes `deletion_tombstone:<tenant>` (tombstone_store._lock does the same, and
--   the retention.delete job shares the import lock before `legal_hold`), so no
--   writer commits inside an import and an open writer holds it off. Lock order:
--   import, then legal_hold, then the stores' locks, then deletion_tombstone. Only
--   the definer (current_user qw_migrate) with the transaction-local
--   `app.tombstones_import` flag keeps a given recorded_at, which must not precede
--   the tenant's latest row (check_violation) nor pass the clock bound (QI003); its
--   deleted_at is held to not after that recorded_at instead of 0057's 60-second
--   window. Any other insert is stamped and checked as before. Locals are
--   pg_catalog types. The function forces custom plans, so the foreign-key and
--   guard lookups planned on the empty ledger do not scan it as it grows (linear).

-- one exported tombstone of tenant t has exactly its shape (tombstone_wire's
-- TOMBSTONE_KEYS and the 0057 and 0059 CHECKs)
CREATE FUNCTION app.portability_tombstone_row(t uuid, w jsonb) RETURNS boolean
LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    ks pg_catalog.text[] := ARRAY['tenant_id', 'seq', 'record_hash', 'cls', 'job_id',
        'requested_at', 'deleted_at', 'residual_backups', 'backup_expiry_at',
        'hash_scheme', 'key_id', 'prev_hash', 'entry_hash', 'recorded_at',
        'row_hash', 'residual_backups_unknown'];
    h pg_catalog.text := '^[0-9a-f]{64}$';
    bk pg_catalog.text[];
    req pg_catalog.timestamptz;
    del pg_catalog.timestamptz;
    rec pg_catalog.timestamptz;
    unknown pg_catalog.bool;
BEGIN
    -- each IF uses only what the IFs before it proved (no cast or array function
    -- on an unchecked value; SQL does not promise the order of an OR)
    IF jsonb_typeof(w) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    IF NOT w ?& ks OR w - ks <> '{}'::jsonb
       OR EXISTS (SELECT 1 FROM unnest(ks) k WHERE k NOT IN ('seq',
           'residual_backups', 'backup_expiry_at', 'residual_backups_unknown')
           AND jsonb_typeof(w -> k) <> 'string')
       OR jsonb_typeof(w -> 'seq') <> 'number'
       OR jsonb_typeof(w -> 'residual_backups') <> 'array'
       OR jsonb_typeof(w -> 'backup_expiry_at') NOT IN ('string', 'null')
       OR jsonb_typeof(w -> 'residual_backups_unknown') <> 'boolean' THEN
        RETURN false;
    END IF;
    IF w ->> 'tenant_id' <> t::text OR (w ->> 'seq') !~ '^(0|[1-9][0-9]{0,9})$'
       OR w ->> 'record_hash' !~ h OR w ->> 'prev_hash' !~ h
       OR w ->> 'entry_hash' !~ h OR w ->> 'row_hash' !~ h
       OR w ->> 'key_id' !~ '^[0-9a-f]{32}$'
       OR w ->> 'hash_scheme' <> 'qw-record-hmac-1'
       OR w ->> 'cls' NOT IN ('security', 'reference', 'financial', 'policy',
           'audit', 'research', 'market', 'evidence', 'release', 'improvement',
           'simulation', 'operations', 'provider_payload', 'log', 'backup')
       OR NOT app.budget_book_id(w -> 'job_id')
       OR jsonb_array_length(w -> 'residual_backups') > 1000
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(w -> 'residual_backups') b
                  WHERE NOT app.budget_book_id(b))
       OR app.budget_book_instant(w -> 'requested_at') IS NULL
       OR app.budget_book_instant(w -> 'deleted_at') IS NULL
       OR app.budget_book_instant(w -> 'recorded_at') IS NULL
       OR (w -> 'backup_expiry_at' <> 'null'::jsonb
           AND app.budget_book_instant(w -> 'backup_expiry_at') IS NULL) THEN
        RETURN false;
    END IF;
    bk := ARRAY(SELECT jsonb_array_elements_text(w -> 'residual_backups'));
    req := app.budget_book_instant(w -> 'requested_at');
    del := app.budget_book_instant(w -> 'deleted_at');
    rec := app.budget_book_instant(w -> 'recorded_at');
    unknown := (w -> 'residual_backups_unknown')::pg_catalog.bool;
    RETURN (w ->> 'seq')::pg_catalog.int8 <= 2147483646
        AND app.deletion_tombstone_backups(bk) AND req <= del AND del <= rec
        AND rec <= clock_timestamp() + interval '5 minutes'
        AND (w -> 'backup_expiry_at' = 'null'::jsonb OR cardinality(bk) > 0)
        AND NOT (unknown AND (cardinality(bk) > 0
                              OR w -> 'backup_expiry_at' <> 'null'::jsonb));
END
$$;

CREATE FUNCTION app.portability_import_tombstones(p jsonb) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp SET plan_cache_mode = force_custom_plan
AS $$
DECLARE
    t pg_catalog.uuid := app.current_tenant_id();
    sk pg_catalog.text[] := ARRAY['ledger', 'tombstones', 'checkpoints'];
    hk pg_catalog.text[] := ARRAY['tenant_id', 'length', 'head', 'hash_scheme',
        'key_id'];
    ck pg_catalog.text[] := ARRAY['tenant_id', 'length', 'head', 'recorded_at',
        'row_hash'];
    h pg_catalog.text := '^[0-9a-f]{64}$';
    w pg_catalog.jsonb;
    c pg_catalog.jsonb;
    prev pg_catalog.text := repeat('0', 64);
    len pg_catalog.int8 := 0;  -- the previous checkpoint's length
    total pg_catalog.int8;
    n pg_catalog.int8 := 0;
    k pg_catalog.int4 := 0;
    sealed pg_catalog.text;
    stamp pg_catalog.timestamptz;
    last record;
BEGIN
    IF t IS NULL THEN
        RAISE EXCEPTION 'portability import: no tenant' USING ERRCODE = 'QI003';
    END IF;
    IF NOT app.portability_lock_held(t) THEN
        RAISE EXCEPTION 'portability import: the import lock is not held'
            USING ERRCODE = 'QI002';
    END IF;
    IF EXISTS (SELECT 1 FROM app.deletion_tombstone x WHERE x.tenant_id = t)
       OR EXISTS (SELECT 1 FROM app.deletion_tombstone_checkpoint x
                  WHERE x.tenant_id = t) THEN
        RAISE EXCEPTION 'portability import: the tombstones are not empty'
            USING ERRCODE = 'QI001';
    END IF;
    IF (CASE WHEN jsonb_typeof(p) IS DISTINCT FROM 'object' THEN true
            ELSE NOT p ?& sk OR p - sk <> '{}'::jsonb
              OR jsonb_typeof(p -> 'tombstones') <> 'array'
              OR jsonb_typeof(p -> 'checkpoints') <> 'array'
              OR jsonb_typeof(p -> 'ledger') <> 'object' END) THEN
        RAISE EXCEPTION 'portability import: the section' USING ERRCODE = 'QI003';
    END IF;
    w := p -> 'ledger';  -- its values are compared with the stored rows below
    IF NOT w ?& hk OR w - hk <> '{}'::jsonb
       OR jsonb_typeof(w -> 'tenant_id') <> 'string'
       OR jsonb_typeof(w -> 'head') <> 'string'
       OR jsonb_typeof(w -> 'length') <> 'number'
       OR jsonb_typeof(w -> 'hash_scheme') NOT IN ('string', 'null')
       OR jsonb_typeof(w -> 'key_id') NOT IN ('string', 'null') THEN
        RAISE EXCEPTION 'portability import: the ledger header'
            USING ERRCODE = 'QI003';
    END IF;
    IF (w ->> 'length') !~ '^(0|[1-9][0-9]{0,9})$' THEN
        RAISE EXCEPTION 'portability import: the ledger header'
            USING ERRCODE = 'QI003';
    END IF;
    total := jsonb_array_length(p -> 'tombstones');
    FOR w IN SELECT x FROM jsonb_array_elements(p -> 'tombstones') WITH ORDINALITY
            a (x, o) ORDER BY a.o LOOP  -- every row, before any insert
        IF (CASE WHEN NOT app.portability_tombstone_row(t, w) THEN true
                ELSE (w ->> 'seq')::pg_catalog.int8 <> n
                  OR w ->> 'prev_hash' <> prev END) THEN
            RAISE EXCEPTION 'portability import: a tombstone row %', n
                USING ERRCODE = 'QI003';
        END IF;
        prev := w ->> 'entry_hash';
        n := n + 1;
    END LOOP;
    FOR c IN SELECT x FROM jsonb_array_elements(p -> 'checkpoints') WITH ORDINALITY
            a (x, o) ORDER BY a.o LOOP
        IF (CASE WHEN jsonb_typeof(c) IS DISTINCT FROM 'object' THEN true
                ELSE NOT c ?& ck OR c - ck <> '{}'::jsonb
                  OR jsonb_typeof(c -> 'length') <> 'number'
                  OR EXISTS (SELECT 1 FROM unnest(ck) x WHERE x <> 'length'
                             AND jsonb_typeof(c -> x) <> 'string') END) THEN
            RAISE EXCEPTION 'portability import: a checkpoint row'
                USING ERRCODE = 'QI003';
        END IF;
        IF c ->> 'tenant_id' <> t::text OR (c ->> 'length') !~ '^[1-9][0-9]{0,9}$'
           OR c ->> 'head' !~ h OR c ->> 'row_hash' !~ h
           OR app.budget_book_instant(c -> 'recorded_at') IS NULL THEN
            RAISE EXCEPTION 'portability import: a checkpoint row'
                USING ERRCODE = 'QI003';
        END IF;
        IF (c ->> 'length')::pg_catalog.int8 NOT BETWEEN len + 1 AND total
           OR app.budget_book_instant(c -> 'recorded_at')
              > clock_timestamp() + interval '5 minutes' THEN
            RAISE EXCEPTION 'portability import: a checkpoint row'
                USING ERRCODE = 'QI003';
        END IF;
        len := (c ->> 'length')::pg_catalog.int8;
    END LOOP;
    IF len <> total THEN
        RAISE EXCEPTION 'portability import: the latest checkpoint does not cover '
            'the ledger' USING ERRCODE = 'QI003';
    END IF;
    PERFORM set_config('app.tombstones_import', 'on', true);  -- the guard's branch
    -- one walk: each entry with the checkpoint at its length (distinct and digits,
    -- checked above), joined once, never rescanned (review MEDIUM-1)
    FOR w, c IN SELECT a.x, b.x FROM jsonb_array_elements(p -> 'tombstones')
            WITH ORDINALITY a (x, o) LEFT JOIN jsonb_array_elements(
                p -> 'checkpoints') b (x)
            ON (b.x ->> 'length')::pg_catalog.int8 = a.o ORDER BY a.o LOOP
        INSERT INTO app.deletion_tombstone AS x (tenant_id, seq, record_hash, cls,
            job_id, requested_at, deleted_at, residual_backups, backup_expiry_at,
            hash_scheme, key_id, prev_hash, entry_hash, recorded_at,
            residual_backups_unknown)
        VALUES (t, (w ->> 'seq')::pg_catalog.int8, w ->> 'record_hash',
            w ->> 'cls', w ->> 'job_id', app.budget_book_instant(w -> 'requested_at'),
            app.budget_book_instant(w -> 'deleted_at'),
            ARRAY(SELECT jsonb_array_elements_text(w -> 'residual_backups')),
            app.budget_book_instant(w -> 'backup_expiry_at'), w ->> 'hash_scheme',
            w ->> 'key_id', w ->> 'prev_hash', w ->> 'entry_hash',
            app.budget_book_instant(w -> 'recorded_at'),
            (w -> 'residual_backups_unknown')::pg_catalog.bool)
        RETURNING x.row_hash, x.recorded_at INTO sealed, stamp;
        IF sealed IS DISTINCT FROM w ->> 'row_hash'
           OR stamp IS DISTINCT FROM app.budget_book_instant(w -> 'recorded_at') THEN
            RAISE EXCEPTION 'portability import: tombstone % does not reproduce',
                w ->> 'seq' USING ERRCODE = 'QI003';
        END IF;
        k := k + 1;
        IF c IS NOT NULL THEN
            INSERT INTO app.deletion_tombstone_checkpoint AS x (tenant_id, length,
                head, recorded_at)
            VALUES (t, (c ->> 'length')::pg_catalog.int8, c ->> 'head',
                app.budget_book_instant(c -> 'recorded_at'))
            RETURNING x.row_hash, x.recorded_at INTO sealed, stamp;
            IF sealed IS DISTINCT FROM c ->> 'row_hash'
               OR stamp IS DISTINCT FROM app.budget_book_instant(c -> 'recorded_at')
            THEN
                RAISE EXCEPTION 'portability import: checkpoint % does not reproduce',
                    c ->> 'length' USING ERRCODE = 'QI003';
            END IF;
            k := k + 1;
        END IF;
    END LOOP;
    PERFORM set_config('app.tombstones_import', '', true);
    -- tombstone_wire.header over the stored rows
    SELECT count(*) AS n, (array_agg(x.entry_hash ORDER BY x.seq DESC))[1] AS head,
           (array_agg(x.hash_scheme ORDER BY x.seq DESC))[1] AS hash_scheme,
           (array_agg(x.key_id ORDER BY x.seq DESC))[1] AS key_id
        INTO last FROM app.deletion_tombstone x WHERE x.tenant_id = t;
    IF jsonb_build_object('tenant_id', t::text, 'length', last.n,
            'head', coalesce(last.head, repeat('0', 64)),
            'hash_scheme', last.hash_scheme, 'key_id', last.key_id)
       IS DISTINCT FROM p -> 'ledger'
       OR (SELECT count(*) FROM app.deletion_tombstone_checkpoint x
           WHERE x.tenant_id = t) <> jsonb_array_length(p -> 'checkpoints') THEN
        RAISE EXCEPTION 'portability import: the ledger header does not reproduce'
            USING ERRCODE = 'QI003';
    END IF;
    RETURN k;
END
$$;

REVOKE ALL ON FUNCTION app.portability_tombstone_row(uuid, jsonb),
    app.portability_import_tombstones(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.portability_import_tombstones(jsonb) TO qw_app;

-- 0059's guard with the import lock first and the import branch; otherwise
-- unchanged
CREATE OR REPLACE FUNCTION app.deletion_tombstone_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    now_ CONSTANT pg_catalog.timestamptz := clock_timestamp();
    imp CONSTANT pg_catalog.bool := current_user = 'qw_migrate'
        AND pg_catalog.current_setting('app.tombstones_import', true) = 'on';
    latest pg_catalog.timestamptz;
    p record;
    n pg_catalog.int8;
    sealed pg_catalog.text;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(hashtextextended(
        'import:' || NEW.tenant_id::text, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'deletion_tombstone:' || NEW.tenant_id::text, 0));
    SELECT seq, entry_hash, deleted_at, key_id, recorded_at INTO p
        FROM app.deletion_tombstone WHERE tenant_id = NEW.tenant_id
        ORDER BY seq DESC LIMIT 1;
    IF imp THEN  -- into an empty ledger, each insert in knowledge order, so each
        -- table's latest stamp is its last row's: no scan (review MEDIUM-1)
        latest := greatest(p.recorded_at, (SELECT c.recorded_at
            FROM app.deletion_tombstone_checkpoint c WHERE c.tenant_id = NEW.tenant_id
            ORDER BY c.length DESC LIMIT 1));
    ELSE
        SELECT max(t) INTO latest FROM (
            SELECT max(recorded_at) t FROM app.deletion_tombstone
                WHERE tenant_id = NEW.tenant_id
            UNION ALL SELECT max(recorded_at) FROM app.deletion_tombstone_checkpoint
                WHERE tenant_id = NEW.tenant_id) x;
    END IF;
    IF imp THEN  -- decision 112: the archive's stamp, in knowledge order
        IF NEW.recorded_at IS NULL OR NEW.recorded_at < latest THEN
            RAISE EXCEPTION 'app.%: not in knowledge order', TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.recorded_at > now_ + interval '5 minutes' THEN
            RAISE EXCEPTION 'app.%: recorded in the future', TG_TABLE_NAME
                USING ERRCODE = 'QI003';
        END IF;
    ELSE
        NEW.recorded_at := greatest(now_, latest);  -- any value given is replaced
    END IF;
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
        IF imp THEN  -- deleted no later than it was recorded (0057 stamps so)
            IF NEW.deleted_at > NEW.recorded_at THEN
                RAISE EXCEPTION 'app.deletion_tombstone: time_order'
                    USING ERRCODE = 'check_violation';
            END IF;
        ELSIF NEW.deleted_at > greatest(now_, p.deleted_at)
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
