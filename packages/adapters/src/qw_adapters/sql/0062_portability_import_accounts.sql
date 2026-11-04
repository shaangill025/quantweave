-- 0062: the destination import of the accounts (T057 increment 8; owner decision
-- 112, as 0060 for the journal). Decision 112 authorises SECURITY DEFINER for these
-- import functions only; 0055's and 0056's own functions stay invoker rights.
-- - app.portability_import_accounts(section): owner qw_migrate, search_path pinned
--   (pg_temp last; pg_catalog's relations named), EXECUTE for qw_app only. The
--   tenant is the transaction's app.current_tenant_id(), never an argument; forced
--   RLS still binds the definer. Refusals, in order: no tenant (QI003); unless this
--   backend holds `import:<tenant>` in exclusive mode (QI002, app.portability_lock_
--   held, the same match as 0060); unless the tenant has no account,
--   account_revision, data_source or source_mapping_revision row (QI001, under that
--   lock); then the section's exact shape (QI003): the four lists of
--   qw_domain.account_wire, each row with exactly its keys, its tenant's id, JSON
--   strings (revision a number, previous_hash a string or null) and a canonical
--   recorded_at no more than 5 minutes past clock_timestamp(), so an archive cannot
--   set the tenant's knowledge floor in the future. A non-object element is QI003
--   too (a CASE guards the object operators). Every row is checked before any
--   insert. Values are read, never built into SQL.
-- - Rows go in one knowledge order: recorded_at, then accounts, sources, mappings,
--   account revisions, then archive order (primary key order), so every foreign key,
--   interval account and chain parent precedes its row. Each keeps the archive's
--   recorded_at; the guard below seals row_hash from it as always and checks both
--   chains (revision n + 1 naming the previous row hash), the lifecycle (archive
--   once, final), the intervals and the tenant's knowledge floor (check_violation).
--   Then the stored row's content_hash is recomputed in SQL as account_wire's digest
--   (json.dumps sort_keys, separators (",", ":"), ensure_ascii False: to_json's
--   escapes are the same) and must equal the archive's, as must row_hash (QI003).
-- - The 0056 guard is replaced. Every insert first shares `import:<tenant>`, then
--   takes `source_map:<tenant>` (qw_adapters.account_store._lock does the same), so
--   no writer of these tables commits while an import holds the lock and an open
--   writer holds the import off; lock order: import, then source_map. Only the
--   definer (current_user qw_migrate) with the transaction-local
--   `app.accounts_import` flag keeps a given recorded_at, which must not precede the
--   tenant's latest row; any other insert is stamped as before. Locals are
--   pg_catalog types. The two helpers have no EXECUTE grant (PUBLIC revoked).
-- - 0060's app.ledger_event_stamp is replaced with the same 5-minute bound in its
--   import branch (QI003), the journal's half of this rule; 0060 itself is
--   applied and checksummed, so it is not edited.

CREATE FUNCTION app.portability_lock_held(t uuid) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $$  -- the advisory lock `import:<t>`, exclusive, held by this backend
    SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks l,
            (SELECT hashtextextended('import:' || t::text, 0) k) x
        WHERE l.locktype = 'advisory' AND l.pid = pg_backend_pid() AND l.granted
          AND l.mode = 'ExclusiveLock'
          AND l.database = (SELECT d.oid FROM pg_catalog.pg_database d
                            WHERE d.datname = current_database())
          AND l.classid = ((x.k >> 32) & 4294967295)::oid
          AND l.objid = (x.k & 4294967295)::oid AND l.objsubid = 1)
$$;

-- account_wire's content digests over a stored row (to_jsonb of it)
CREATE FUNCTION app.portability_account_content(t uuid, tbl text, r jsonb)
RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $$
    SELECT encode(sha256(convert_to('[' || app.portability_json(tbl) || ','
        || app.portability_json(t::text) || ',' || CASE tbl
        WHEN 'account' THEN concat_ws(',', app.portability_json(r ->> 'account_id'),
            app.portability_json(r ->> 'name'), app.portability_json(r ->> 'broker'),
            app.portability_json(r ->> 'account_type'),
            app.portability_json(r ->> 'reporting_currency'),
            app.portability_json(r ->> 'analysis_scope'))
        WHEN 'data_source' THEN concat_ws(',', app.portability_json(r ->> 'source_id'),
            app.portability_json(r ->> 'kind'), app.portability_json(r ->> 'provider'))
        WHEN 'account_revision' THEN concat_ws(',',
            app.portability_json(r ->> 'account_id'), (r ->> 'revision')::integer,
            app.portability_json(r ->> 'lifecycle'),
            app.portability_json(r ->> 'previous_hash'))
        ELSE concat_ws(',', app.portability_json(r ->> 'source_id'),
            app.portability_json(r ->> 'account_ref'), (r ->> 'revision')::integer,
            '[' || coalesce((SELECT string_agg('{"account_id":'
                || app.portability_json(i ->> 'account_id') || ',"valid_from":'
                || app.portability_json(i ->> 'valid_from') || ',"valid_to":'
                || app.portability_json(i ->> 'valid_to') || '}', ',' ORDER BY o)
                FROM jsonb_array_elements(r -> 'intervals') WITH ORDINALITY x (i, o)),
                '') || ']',
            app.portability_json(r ->> 'previous_hash'))
        END || ']', 'UTF8')), 'hex')
$$;

CREATE FUNCTION app.portability_import_accounts(p jsonb) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    t pg_catalog.uuid := app.current_tenant_id();
    lists pg_catalog.text[] := ARRAY['accounts', 'sources', 'mappings',
        'account_revisions'];
    todo pg_catalog.jsonb;  -- the rows in knowledge order, all checked first
    rt pg_catalog.text;
    w pg_catalog.jsonb;
    ks pg_catalog.text[];
    r pg_catalog.jsonb;
    n pg_catalog.int4 := 0;
BEGIN
    IF t IS NULL THEN
        RAISE EXCEPTION 'portability import: no tenant' USING ERRCODE = 'QI003';
    END IF;
    IF NOT app.portability_lock_held(t) THEN
        RAISE EXCEPTION 'portability import: the import lock is not held'
            USING ERRCODE = 'QI002';
    END IF;
    IF EXISTS (SELECT 1 FROM app.account x WHERE x.tenant_id = t)
       OR EXISTS (SELECT 1 FROM app.account_revision x WHERE x.tenant_id = t)
       OR EXISTS (SELECT 1 FROM app.data_source x WHERE x.tenant_id = t)
       OR EXISTS (SELECT 1 FROM app.source_mapping_revision x WHERE x.tenant_id = t)
    THEN
        RAISE EXCEPTION 'portability import: the accounts are not empty'
            USING ERRCODE = 'QI001';
    END IF;
    IF jsonb_typeof(p) IS DISTINCT FROM 'object' OR NOT p ?& lists
       OR p - lists <> '{}'::jsonb OR EXISTS (SELECT 1 FROM unnest(lists) k
            WHERE jsonb_typeof(p -> k) <> 'array') THEN
        RAISE EXCEPTION 'portability import: the section' USING ERRCODE = 'QI003';
    END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_array(l.tbl, a.e, l.keys
                || ARRAY['tenant_id', 'recorded_at', 'content_hash', 'row_hash'])
            ORDER BY app.budget_book_instant(a.e -> 'recorded_at'), l.rank, a.o), '[]')
        INTO todo
        FROM (VALUES
            ('account', 1, 'accounts', ARRAY['account_id', 'name', 'broker',
                'account_type', 'reporting_currency', 'analysis_scope']),
            ('data_source', 2, 'sources', ARRAY['source_id', 'kind', 'provider']),
            ('source_mapping_revision', 3, 'mappings', ARRAY['source_id',
                'account_ref', 'revision', 'intervals', 'previous_hash']),
            ('account_revision', 4, 'account_revisions', ARRAY['account_id',
                'revision', 'lifecycle', 'previous_hash'])
        ) l (tbl, rank, list, keys),
        jsonb_array_elements(p -> l.list) WITH ORDINALITY a (e, o);
    FOR rt, w, ks IN SELECT x ->> 0, x -> 1,
            ARRAY(SELECT jsonb_array_elements_text(x -> 2))
            FROM jsonb_array_elements(todo) x LOOP
        -- CASE, not OR: no object operator sees a scalar or array element (the
        -- parentheses: plpgsql's IF reads to the first THEN)
        IF (CASE WHEN jsonb_typeof(w) IS DISTINCT FROM 'object' THEN true
                ELSE NOT w ?& ks OR w - ks <> '{}'::jsonb
                  OR w ->> 'tenant_id' IS DISTINCT FROM t::text
                  OR app.budget_book_instant(w -> 'recorded_at') IS NULL
                  -- no knowledge floor in the future (5 minutes of clock skew)
                  OR app.budget_book_instant(w -> 'recorded_at')
                     > clock_timestamp() + interval '5 minutes'
                  OR EXISTS (SELECT 1 FROM jsonb_each(w) j WHERE CASE j.key
                    WHEN 'revision' THEN jsonb_typeof(j.value) <> 'number'
                    WHEN 'intervals' THEN false  -- the guard checks every interval
                    WHEN 'previous_hash' THEN jsonb_typeof(j.value) NOT IN ('string',
                        'null')
                    ELSE jsonb_typeof(j.value) <> 'string' END) END) THEN
            RAISE EXCEPTION 'portability import: a % row', rt
                USING ERRCODE = 'QI003';
        END IF;
    END LOOP;
    PERFORM set_config('app.accounts_import', 'on', true);  -- the guard's branch
    FOR rt, w IN SELECT x ->> 0, x -> 1 FROM jsonb_array_elements(todo) x LOOP
        IF rt = 'account' THEN
            INSERT INTO app.account AS x (tenant_id, account_id, name, broker,
                account_type, reporting_currency, analysis_scope, recorded_at,
                content_hash)
            VALUES (t, w ->> 'account_id', w ->> 'name', w ->> 'broker',
                w ->> 'account_type', w ->> 'reporting_currency',
                w ->> 'analysis_scope', app.budget_book_instant(w -> 'recorded_at'),
                w ->> 'content_hash')
            RETURNING to_jsonb(x.*) INTO r;
        ELSIF rt = 'data_source' THEN
            INSERT INTO app.data_source AS x (tenant_id, source_id, kind, provider,
                recorded_at, content_hash)
            VALUES (t, w ->> 'source_id', w ->> 'kind', w ->> 'provider',
                app.budget_book_instant(w -> 'recorded_at'), w ->> 'content_hash')
            RETURNING to_jsonb(x.*) INTO r;
        ELSIF rt = 'source_mapping_revision' THEN
            INSERT INTO app.source_mapping_revision AS x (tenant_id, source_id,
                account_ref, revision, intervals, recorded_at, previous_hash,
                content_hash)
            VALUES (t, w ->> 'source_id', w ->> 'account_ref',
                (w ->> 'revision')::integer, w -> 'intervals',
                app.budget_book_instant(w -> 'recorded_at'), w ->> 'previous_hash',
                w ->> 'content_hash')
            RETURNING to_jsonb(x.*) INTO r;
        ELSE
            INSERT INTO app.account_revision AS x (tenant_id, account_id, revision,
                lifecycle, recorded_at, previous_hash, content_hash)
            VALUES (t, w ->> 'account_id', (w ->> 'revision')::integer,
                w ->> 'lifecycle', app.budget_book_instant(w -> 'recorded_at'),
                w ->> 'previous_hash', w ->> 'content_hash')
            RETURNING to_jsonb(x.*) INTO r;
        END IF;
        IF r ->> 'row_hash' IS DISTINCT FROM w ->> 'row_hash'
           OR app.portability_account_content(t, rt, r)
              IS DISTINCT FROM w ->> 'content_hash' THEN
            RAISE EXCEPTION 'portability import: % row % does not reproduce', rt, n
                USING ERRCODE = 'QI003';
        END IF;
        n := n + 1;
    END LOOP;
    PERFORM set_config('app.accounts_import', '', true);
    RETURN n;
END
$$;

REVOKE ALL ON FUNCTION app.portability_lock_held(uuid),
    app.portability_account_content(uuid, text, jsonb),
    app.portability_import_accounts(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.portability_import_accounts(jsonb) TO qw_app;

-- 0056's guard with the import lock first and the import branch; otherwise
-- unchanged
CREATE OR REPLACE FUNCTION app.source_map_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    latest pg_catalog.timestamptz;
    p record;
    bad pg_catalog.bool;
    prev_rev pg_catalog.int4;
    prev_hash pg_catalog.text;
    prev_life pg_catalog.text;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(hashtextextended(
        'import:' || NEW.tenant_id::text, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'source_map:' || NEW.tenant_id::text, 0));
    SELECT max(x.t) INTO latest FROM (
        SELECT max(recorded_at) t FROM app.account WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.data_source
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.source_mapping_revision
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.account_revision
            WHERE tenant_id = NEW.tenant_id) x;
    IF current_user = 'qw_migrate'
       AND pg_catalog.current_setting('app.accounts_import', true) = 'on' THEN
        IF NEW.recorded_at IS NULL OR NEW.recorded_at < latest THEN
            RAISE EXCEPTION 'app.%: not in knowledge order', TG_TABLE_NAME
                USING ERRCODE = 'check_violation';
        END IF;
    ELSE
        NEW.recorded_at := greatest(clock_timestamp(), latest);  -- any value given
    END IF;
    NEW.row_hash := encode(sha256(convert_to(NEW.content_hash || '|'
        || to_char(NEW.recorded_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN date_trunc('second', NEW.recorded_at) = NEW.recorded_at
                THEN '' ELSE to_char(NEW.recorded_at AT TIME ZONE 'UTC', '.US') END
        || 'Z', 'UTF8')), 'hex');
    IF TG_TABLE_NAME = 'account_revision' THEN
        SELECT revision, row_hash, lifecycle INTO prev_rev, prev_hash, prev_life
            FROM app.account_revision
            WHERE tenant_id = NEW.tenant_id AND account_id = NEW.account_id
            ORDER BY revision DESC LIMIT 1;
        IF prev_rev IS NULL THEN
            SELECT 1, row_hash, 'active' INTO prev_rev, prev_hash, prev_life
                FROM app.account
                WHERE tenant_id = NEW.tenant_id AND account_id = NEW.account_id;
        END IF;
        IF prev_life = 'archived' THEN
            RAISE EXCEPTION 'app.account_revision: archived'
                USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.revision IS DISTINCT FROM prev_rev + 1
           OR NEW.previous_hash IS DISTINCT FROM prev_hash THEN
            RAISE EXCEPTION 'app.account_revision: not the next revision'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME <> 'source_mapping_revision' THEN
        RETURN NEW;
    END IF;
    SELECT revision, row_hash INTO p FROM app.source_mapping_revision
        WHERE tenant_id = NEW.tenant_id AND source_id = NEW.source_id
          AND account_ref = NEW.account_ref
        ORDER BY revision DESC LIMIT 1;
    IF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.row_hash THEN
        RAISE EXCEPTION 'app.source_map: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    -- each interval: exact keys, canonical instants, an account of this tenant,
    -- non-empty; sorted by valid_from and pairwise disjoint
    WITH iv AS (
        SELECT n, e, app.budget_book_instant(e -> 'valid_from') f,
               CASE WHEN e -> 'valid_to' = 'null'::jsonb THEN 'infinity'::timestamptz
                    ELSE app.budget_book_instant(e -> 'valid_to') END t
        FROM jsonb_array_elements(NEW.intervals) WITH ORDINALITY x (e, n))
    SELECT bool_or(NOT coalesce(jsonb_typeof(e) = 'object'
            AND e ?& ARRAY['account_id', 'valid_from', 'valid_to']
            AND e - ARRAY['account_id', 'valid_from', 'valid_to'] = '{}'::jsonb
            AND f IS NOT NULL AND t IS NOT NULL AND t > f
            AND EXISTS (SELECT 1 FROM app.account a WHERE a.tenant_id = NEW.tenant_id
                        AND to_jsonb(a.account_id) = e -> 'account_id'), false))
        OR EXISTS (SELECT 1 FROM iv a JOIN iv b ON a.n < b.n
                   WHERE b.f < a.f OR tstzrange(a.f, a.t) && tstzrange(b.f, b.t))
        INTO bad FROM iv;
    IF bad THEN
        RAISE EXCEPTION 'app.source_map: intervals' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

-- 0060's journal stamp trigger with the same upper bound on an imported
-- recorded_at (QI003) and pg_catalog local types; otherwise unchanged
CREATE OR REPLACE FUNCTION app.ledger_event_stamp() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    latest pg_catalog.timestamptz;
    rev pg_catalog.int8;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(
        hashtextextended('import:' || NEW.tenant_id::text, 0));
    PERFORM pg_advisory_xact_lock(
        hashtextextended('ledger:' || NEW.tenant_id || ':' || NEW.account_id, 0));
    SELECT max(e.recorded_at), coalesce(max(e.account_rev), 0) INTO latest, rev
        FROM app.ledger_event e
        WHERE e.tenant_id = NEW.tenant_id AND e.account_id = NEW.account_id;
    IF current_user = 'qw_migrate'
       AND pg_catalog.current_setting('app.journal_import', true) = 'on' THEN
        IF NEW.account_rev IS DISTINCT FROM rev + 1 OR NEW.recorded_at IS NULL
           OR NEW.recorded_at < latest THEN
            RAISE EXCEPTION 'ledger event %: not the next imported revision',
                NEW.event_id USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.recorded_at > clock_timestamp() + interval '5 minutes' THEN
            RAISE EXCEPTION 'ledger event %: recorded in the future', NEW.event_id
                USING ERRCODE = 'QI003';
        END IF;
    ELSE
        NEW.recorded_at := greatest(clock_timestamp(), latest);
        NEW.account_rev := rev + 1;
    END IF;
    NEW.tx_id := pg_current_xact_id();
    RETURN NEW;
END
$$;
