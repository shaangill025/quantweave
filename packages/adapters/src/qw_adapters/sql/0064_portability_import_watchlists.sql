-- 0064: the destination import of the watchlists (T057 increment 9; owner decisions
-- 112, 116, 125 and 126; as 0060 and 0062). Decision 112 authorises SECURITY DEFINER
-- for these import functions only; 0022's guard stays invoker rights. Extends 0022,
-- 0023 and 0024, which are not edited.
-- - The archive holds each live list's latest state at its revision n, with no
--   history and no times (T057 increment 3). It is imported as one 'import' row at
--   revision n with no previous hash: the first row of its chain, the only action
--   besides 'create' that starts one, and only in the definer. The store replays it
--   as the list's state at n; later revisions count on from it.
-- - app.portability_import_watchlists(section, export_id, principal, session):
--   owner qw_migrate, search_path pinned (pg_temp last; pg_catalog's relations
--   named), EXECUTE for qw_app only; the tenant is the transaction's
--   app.current_tenant_id(), never an argument, and forced RLS still binds the
--   definer. Refusals, in order: no tenant (QI003); unless this backend holds
--   `import:<tenant>` exclusive (QI002, 0062's app.portability_lock_held); unless
--   the tenant has no watchlist_revision row (QI001, under that lock); unless the
--   session is the principal's, a tenant owner's, live and stepped up within the
--   hour (QI004; decision 125, 0009's and 0025's window); then the whole shape
--   (QI003) before any insert: an array of objects with exactly the export keys, its
--   tenant's id, canonical UUID ids, unique and in order, an integer revision, the
--   0022 text bounds, and each entry and condition with exactly its keys, JSON
--   types, integer priorities, string arrays, canonical review times and
--   instruments in strict order (app.portability_watchlist_row). Values are read,
--   never built into SQL.
-- - Each row: made by the principal in the session (decision 116), keyed 'imp' ||
--   hex sha256('import:<export_id>:<watchlist_id>:<revision>') (decision 126, so a
--   retry reuses the key), recorded_at the destination clock (the archive has no
--   time; 0022's 60-second rule applies). event_hash is computed in SQL as
--   qw_adapters.watchlist_store.revision_hash (json.dumps sort_keys, compact,
--   ensure_ascii: app.portability_ascii_json), then recomputed from the stored row
--   and compared with the archive's content (QI003).
-- - 0022's guard is replaced: every insert first shares `import:<tenant>`, then
--   takes `watchlist:<tenant>` (watchlist_store._lock does the same), so no writer
--   commits inside an import and an open writer holds it off. An 'import' row is
--   accepted only from the definer (current_user qw_migrate) with the
--   transaction-local `app.watchlists_import` flag, and only as a list's first row;
--   any other row is checked as before. Locals are pg_catalog types.

ALTER TABLE app.watchlist_revision
    DROP CONSTRAINT watchlist_revision_action_check,
    ADD CONSTRAINT watchlist_revision_action_check CHECK (action IN ('create',
        'update', 'delete', 'add_entry', 'update_entry', 'remove_entry', 'import')),
    DROP CONSTRAINT watchlist_revision_check,
    DROP CONSTRAINT watchlist_revision_check1,
    ADD CONSTRAINT watchlist_revision_first CHECK (
        (previous_hash IS NULL) = (action IN ('create', 'import'))),
    ADD CONSTRAINT watchlist_revision_create_first CHECK (
        (action = 'create') <= (revision = 1)),
    DROP CONSTRAINT watchlist_revision_instrument,
    ADD CONSTRAINT watchlist_revision_instrument CHECK ((instrument_id IS NULL) = (
        action IN ('create', 'update', 'delete', 'import')));

-- json.dumps(s) (ensure_ascii): \" \\ \b \f \n \r \t, other non-printable or
-- non-ASCII as lowercase \uXXXX, surrogate pairs past the BMP
CREATE FUNCTION app.portability_ascii(s text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $$
    SELECT CASE WHEN s IS NULL THEN 'null' ELSE '"' || coalesce((SELECT string_agg(
        CASE WHEN c = '"' THEN '\"' WHEN c = '\' THEN '\\'
            WHEN c = chr(8) THEN '\b' WHEN c = chr(12) THEN '\f'
            WHEN c = chr(10) THEN '\n' WHEN c = chr(13) THEN '\r'
            WHEN c = chr(9) THEN '\t'
            WHEN ascii(c) BETWEEN 32 AND 126 THEN c
            WHEN ascii(c) < 65536 THEN '\u' || lpad(to_hex(ascii(c)), 4, '0')
            ELSE '\u' || to_hex(55296 + ((ascii(c) - 65536) >> 10))
                || '\u' || to_hex(56320 + ((ascii(c) - 65536) & 1023)) END,
        '' ORDER BY o) FROM string_to_table(s, NULL) WITH ORDINALITY x (c, o)), '')
        || '"' END
$$;

-- json.dumps(v, sort_keys=True, separators=(",", ":")) with ensure_ascii; keys in
-- code point order (COLLATE "C" on UTF-8); numbers as stored (integers, checked)
CREATE FUNCTION app.portability_ascii_json(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RETURN CASE jsonb_typeof(v)
        WHEN 'object' THEN '{' || coalesce((SELECT string_agg(app.portability_ascii(k)
            || ':' || app.portability_ascii_json(e), ',' ORDER BY k COLLATE "C")
            FROM jsonb_each(v) x (k, e)), '') || '}'
        WHEN 'array' THEN '[' || coalesce((SELECT string_agg(
            app.portability_ascii_json(e), ',' ORDER BY o)
            FROM jsonb_array_elements(v) WITH ORDINALITY x (e, o)), '') || ']'
        WHEN 'string' THEN app.portability_ascii(v #>> '{}')
        ELSE v::text END;
END
$$;

-- watchlist_store.revision_hash over a stored row
CREATE FUNCTION app.portability_watchlist_hash(r app.watchlist_revision)
RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $$
    SELECT encode(sha256(convert_to('[' || concat_ws(',',
        app.portability_ascii('watchlist_revision'),
        app.portability_ascii(r.tenant_id::text),
        app.portability_ascii(r.watchlist_id::text), r.revision::text,
        app.portability_ascii(r.action), app.portability_ascii(r.principal_id::text),
        app.portability_ascii(r.session_id::text),
        app.portability_ascii(r.idempotency_key), app.portability_ascii(r.name),
        app.portability_ascii(r.description), app.portability_ascii_json(r.entries),
        app.portability_ascii(app.portability_instant(r.recorded_at)),
        app.portability_ascii(r.previous_hash))
        || CASE WHEN r.instrument_id IS NULL THEN ''
            ELSE ',' || app.portability_ascii(r.instrument_id::text) END
        || CASE WHEN r.review_at_set THEN ',"review_at_set"' ELSE '' END
        || ']', 'UTF8')), 'hex')
$$;

-- one exported list of tenant t has exactly its shape (the export section's keys
-- and JSON types; qw_domain.portability._WATCHLIST)
CREATE FUNCTION app.portability_watchlist_row(t uuid, w jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    u pg_catalog.text := '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';
    lk pg_catalog.text[] := ARRAY['id', 'tenant_id', 'revision', 'name',
        'description', 'entries'];
    ek pg_catalog.text[] := ARRAY['instrument_id', 'thesis', 'tags', 'review_at',
        'monitoring_priority', 'conditions', 'invalidation_text', 'needs'];
    ck pg_catalog.text[] := ARRAY['metric', 'operator', 'values', 'data_spec_id',
        'description'];
    e pg_catalog.jsonb;
    c pg_catalog.jsonb;
    prev_id pg_catalog.text := '';
BEGIN
    -- each IF uses only what the IFs before it proved (no cast or array function
    -- on an unchecked value; SQL does not promise the order of an OR)
    IF jsonb_typeof(w) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
    IF NOT w ?& lk OR w - lk <> '{}'::jsonb
       OR EXISTS (SELECT 1 FROM unnest(ARRAY['id', 'tenant_id', 'name',
           'description']) k WHERE jsonb_typeof(w -> k) <> 'string')
       OR jsonb_typeof(w -> 'revision') <> 'number'
       OR jsonb_typeof(w -> 'entries') <> 'array' THEN
        RETURN false;
    END IF;
    IF w ->> 'tenant_id' <> t::text OR w ->> 'id' !~ u
       OR (w ->> 'revision') !~ '^[1-9][0-9]{0,9}$'
       OR char_length(w ->> 'name') NOT BETWEEN 1 AND 200
       OR char_length(w ->> 'description') > 4000
       OR jsonb_array_length(w -> 'entries') > 500 THEN
        RETURN false;
    END IF;
    IF (w ->> 'revision')::pg_catalog.int8 > 2147483646 THEN RETURN false; END IF;
    FOR e IN SELECT x FROM jsonb_array_elements(w -> 'entries') x LOOP
        IF jsonb_typeof(e) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
        IF NOT e ?& ek OR e - ek <> '{}'::jsonb
           OR EXISTS (SELECT 1 FROM unnest(ARRAY['instrument_id', 'thesis',
               'invalidation_text', 'needs']) k WHERE jsonb_typeof(e -> k) <> 'string')
           OR jsonb_typeof(e -> 'monitoring_priority') <> 'number'
           OR jsonb_typeof(e -> 'tags') <> 'array'
           OR jsonb_typeof(e -> 'conditions') <> 'array'
           OR NOT (e -> 'review_at' = 'null'::jsonb
                   OR app.budget_book_instant(e -> 'review_at') IS NOT NULL) THEN
            RETURN false;
        END IF;
        IF e ->> 'instrument_id' !~ u
           OR (e ->> 'instrument_id') COLLATE "C" <= prev_id COLLATE "C"
           OR (e ->> 'monitoring_priority') !~ '^(0|[1-9][0-9]?|100)$'
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(e -> 'tags') g
                      WHERE jsonb_typeof(g) <> 'string') THEN
            RETURN false;
        END IF;
        prev_id := e ->> 'instrument_id';
        FOR c IN SELECT x FROM jsonb_array_elements(e -> 'conditions') x LOOP
            IF jsonb_typeof(c) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
            IF NOT c ?& ck OR c - ck <> '{}'::jsonb
               OR EXISTS (SELECT 1 FROM unnest(ck) k WHERE k <> 'values'
                          AND jsonb_typeof(c -> k) <> 'string')
               OR jsonb_typeof(c -> 'values') <> 'array' THEN
                RETURN false;
            END IF;
            IF EXISTS (SELECT 1 FROM jsonb_array_elements(c -> 'values') v
                       WHERE jsonb_typeof(v) <> 'string') THEN
                RETURN false;
            END IF;
        END LOOP;
    END LOOP;
    RETURN true;
END
$$;

CREATE FUNCTION app.portability_import_watchlists(p jsonb, p_export text,
    p_principal uuid, p_session uuid) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    t pg_catalog.uuid := app.current_tenant_id();
    w pg_catalog.jsonb;
    r app.watchlist_revision;
    prev_id pg_catalog.text := '';
    n pg_catalog.int4 := 0;
BEGIN
    IF t IS NULL THEN
        RAISE EXCEPTION 'portability import: no tenant' USING ERRCODE = 'QI003';
    END IF;
    IF NOT app.portability_lock_held(t) THEN
        RAISE EXCEPTION 'portability import: the import lock is not held'
            USING ERRCODE = 'QI002';
    END IF;
    IF EXISTS (SELECT 1 FROM app.watchlist_revision x WHERE x.tenant_id = t) THEN
        RAISE EXCEPTION 'portability import: the watchlists are not empty'
            USING ERRCODE = 'QI001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.session s JOIN app.membership m
            ON m.tenant_id = s.tenant_id AND m.user_id = s.user_id
            WHERE s.tenant_id = t AND s.id = p_session AND s.user_id = p_principal
              AND m.role = 'tenant_owner' AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND s.step_up_at >= now() - interval '1 hour') THEN
        RAISE EXCEPTION 'portability import: not the owner''s step-up session'
            USING ERRCODE = 'QI004';
    END IF;
    IF jsonb_typeof(p) IS DISTINCT FROM 'array'
       OR coalesce(p_export !~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$', true) THEN
        RAISE EXCEPTION 'portability import: the section' USING ERRCODE = 'QI003';
    END IF;
    FOR w IN SELECT x FROM jsonb_array_elements(p) WITH ORDINALITY a (x, o)
            ORDER BY a.o LOOP  -- every row, before any insert
        IF NOT app.portability_watchlist_row(t, w)
           OR (w ->> 'id') COLLATE "C" <= prev_id COLLATE "C" THEN
            RAISE EXCEPTION 'portability import: a watchlist row %', n
                USING ERRCODE = 'QI003';
        END IF;
        prev_id := w ->> 'id';
        n := n + 1;
    END LOOP;
    n := 0;
    PERFORM set_config('app.watchlists_import', 'on', true);  -- the guard's branch
    FOR w IN SELECT x FROM jsonb_array_elements(p) WITH ORDINALITY a (x, o)
            ORDER BY a.o LOOP
        r := ROW(t, (w ->> 'id')::pg_catalog.uuid,
            (w ->> 'revision')::pg_catalog.int4, 'import', p_principal, p_session,
            'imp' || encode(sha256(convert_to('import:' || p_export || ':'
                || (w ->> 'id') || ':' || (w ->> 'revision'), 'UTF8')), 'hex'),
            w ->> 'name', w ->> 'description', w -> 'entries', clock_timestamp(),
            NULL, NULL, NULL, false)::app.watchlist_revision;
        r.event_hash := app.portability_watchlist_hash(r);
        INSERT INTO app.watchlist_revision AS x (tenant_id, watchlist_id, revision,
            action, principal_id, session_id, idempotency_key, name, description,
            entries, recorded_at, previous_hash, event_hash, instrument_id,
            review_at_set)
        VALUES (r.tenant_id, r.watchlist_id, r.revision, r.action, r.principal_id,
            r.session_id, r.idempotency_key, r.name, r.description, r.entries,
            r.recorded_at, NULL, r.event_hash, NULL, false)
        RETURNING x.* INTO r;
        IF app.portability_watchlist_hash(r) IS DISTINCT FROM r.event_hash
           OR (r.name, r.description, r.entries)
              IS DISTINCT FROM (w ->> 'name', w ->> 'description', w -> 'entries')
        THEN
            RAISE EXCEPTION 'portability import: watchlist row % does not reproduce',
                n USING ERRCODE = 'QI003';
        END IF;
        n := n + 1;
    END LOOP;
    PERFORM set_config('app.watchlists_import', '', true);
    RETURN n;
END
$$;

REVOKE ALL ON FUNCTION app.portability_ascii(text),
    app.portability_ascii_json(jsonb),
    app.portability_watchlist_hash(app.watchlist_revision),
    app.portability_watchlist_row(uuid, jsonb),
    app.portability_import_watchlists(jsonb, text, uuid, uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.portability_import_watchlists(jsonb, text, uuid, uuid)
    TO qw_app;

-- 0022's guard with the import lock first and the import branch; otherwise
-- unchanged
CREATE OR REPLACE FUNCTION app.watchlist_revision_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    p record;
    s record;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(hashtextextended(
        'import:' || NEW.tenant_id::text, 0));
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'watchlist:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.watchlist_revision: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT revision, action, recorded_at, event_hash INTO p
        FROM app.watchlist_revision
        WHERE tenant_id = NEW.tenant_id AND watchlist_id = NEW.watchlist_id
        ORDER BY revision DESC LIMIT 1;
    IF NEW.action = 'import' THEN
        IF current_user IS DISTINCT FROM 'qw_migrate'
           OR pg_catalog.current_setting('app.watchlists_import', true)
              IS DISTINCT FROM 'on'
           OR p.revision IS NOT NULL OR NEW.previous_hash IS NOT NULL THEN
            RAISE EXCEPTION 'app.watchlist_revision: not an import'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at OR p.action = 'delete' THEN
        RAISE EXCEPTION 'app.watchlist_revision: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT user_id, revoked_at, expires_at INTO s FROM app.session
        WHERE tenant_id = NEW.tenant_id AND id = NEW.session_id;
    IF NOT FOUND OR s.user_id <> NEW.principal_id OR s.revoked_at IS NOT NULL
       OR s.expires_at <= now() THEN
        RAISE EXCEPTION 'app.watchlist_revision: not the principal''s live session'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;
