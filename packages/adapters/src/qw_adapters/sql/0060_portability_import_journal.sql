-- 0060: the destination import of the journal (T057 increment 7; owner decision
-- 112: per-table SECURITY DEFINER import functions granted to qw_app, each refusing
-- unless the tenant is empty and the import lock is held, re-verifying chains and
-- seals in SQL and inserting with the original stamps; no new login or secret).
-- Decision 112 authorises SECURITY DEFINER for these import functions only; the
-- 0007, 0008 and 0009 headers ("No SECURITY DEFINER function") still hold for
-- their own functions, which stay invoker rights.
-- - app.portability_import_journal(events, chains): owner qw_migrate, search_path
--   pinned, EXECUTE for qw_app only. The tenant is the transaction's
--   app.current_tenant_id() (never an argument), and RLS still binds the definer
--   (0007 forces it). It refuses (SQLSTATE QI002) unless this backend holds the
--   transaction or session advisory lock `import:<tenant>` (the key
--   qw_adapters.destination_import takes) in exclusive mode, then (QI001) unless the
--   tenant has no ledger_event and no source_record. Arguments are fixed JSON
--   shapes read as values; no dynamic SQL is built (QI003 for any other shape).
-- - Each event is inserted with the archive's account_rev and recorded_at (in
--   format_instant form), its postings in their stored line order; then its
--   event_id must be recomputed from the stored rows exactly as
--   qw_domain.postings.JournalEvent computes it (QI003). After all events, the
--   chain header (qw_domain.ledger_wire.chains) is recomputed from the stored rows
--   and must equal the given `chains` exactly (QI003): it binds the tenant id,
--   each position, id and recorded_at. The 0007 balance/reversal check is then
--   run immediately, so every refusal happens inside the caller's savepoint.
-- - The stamp trigger is replaced: in the definer only (current_user qw_migrate
--   and the `app.journal_import` flag the function sets while it inserts; a
--   runtime session that sets the flag itself is still stamped), the given account_rev must be latest + 1 and
--   recorded_at never before the account's latest (check_violation); otherwise it
--   stamps as before. Every journal insert now first takes the tenant's import
--   lock in shared mode, so no journal writer commits while an import holds it,
--   and an open writer holds the import off (the receipt's hard gate, journal).
-- - Lock order, everywhere: `import:<tenant>` (shared, or exclusive for the
--   import), then `ledger:<tenant>:<account>`. The stamp trigger, the replaced
--   0014 virtual_account_guard and qw_adapters.journal_store.lock_account (which
--   every Python taker of an account lock calls) each share the import lock before
--   the account lock, so a writer waits holding no lock the import needs.
-- - The import functions search pg_temp last and name pg_catalog's relations, so
--   a temporary table (TEMPORARY is revoked by 0001) can never stand in for one.
--   The two replaced trigger functions keep 0007's and 0014's setting: they name
--   no unqualified relation.

CREATE FUNCTION app.portability_instant(t timestamptz) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $$  -- qw_domain.instants.format_instant
    SELECT to_char(u, 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN date_trunc('second', u) = u THEN '' ELSE to_char(u, '.US') END
        || 'Z'
    FROM (SELECT t AT TIME ZONE 'UTC' AS u) x
$$;

CREATE FUNCTION app.portability_json(v text) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $$ SELECT coalesce(to_json(v)::text, 'null') $$;  -- a JSON string, or null

-- sha256 of json.dumps(content, sort_keys=True, separators=(",", ":")) over the
-- stored event and lines (every value is ASCII by 0007's checks, so the escapes
-- match Python's ensure_ascii output).
CREATE FUNCTION app.portability_event_id(e app.ledger_event) RETURNS text
LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp
AS $$
    SELECT encode(sha256(convert_to(
        '{"account":' || app.portability_json(e.account_id)
        || ',"effective_at":' || app.portability_json(
            app.portability_instant(e.effective_at))
        || ',"fee":' || CASE WHEN e.terms ? 'fee' THEN '{"amount":'
            || app.portability_json(e.terms -> 'fee' ->> 'amount') || ',"currency":'
            || app.portability_json(e.terms -> 'fee' ->> 'currency') || '}'
            ELSE 'null' END
        || ',"kind":' || app.portability_json(e.kind::text)
        || ',"lot_policy":' || app.portability_json(e.lot_policy)
        || ',"money":[' || coalesce((SELECT string_agg('[' || app.portability_json(a)
            || ',' || app.portability_json(c) || ',' || app.portability_json(w)
            || ',' || app.portability_json(i) || ']', ',' ORDER BY a, c, w, i)
            FROM (SELECT p.book_account::text COLLATE "C" a, p.currency COLLATE "C" c,
                         p.wire COLLATE "C" w,
                         coalesce(p.instrument_id::text, '') COLLATE "C" i
                  FROM app.posting p WHERE p.tenant_id = e.tenant_id
                    AND p.event_id = e.event_id) m), '')
        || '],"reverses":' || app.portability_json(e.reverses)
        || ',"source":[' || app.portability_json(e.source_id) || ','
            || app.portability_json(e.source_record_id) || ']'
        || ',"supersedes":' || app.portability_json(e.supersedes)
        || ',"units":[' || coalesce((SELECT string_agg('[' || app.portability_json(a)
            || ',' || app.portability_json(i) || ',' || app.portability_json(w) || ']',
            ',' ORDER BY a, i, w)
            FROM (SELECT p.unit_account::text COLLATE "C" a,
                         p.instrument_id::text COLLATE "C" i, p.wire COLLATE "C" w
                  FROM app.unit_posting p WHERE p.tenant_id = e.tenant_id
                    AND p.event_id = e.event_id) u), '')
        || ']}', 'UTF8')), 'hex')
$$;

-- qw_domain.ledger_wire.chains over the stored rows: one header per account in
-- byte order of id (json.dumps with separators (",", ":")).
CREATE FUNCTION app.portability_chain(t uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    acct text;
    r record;
    h text;
    n bigint;
    out jsonb := '[]';
BEGIN
    FOR acct IN SELECT DISTINCT e.account_id COLLATE "C" FROM app.ledger_event e
            WHERE e.tenant_id = t ORDER BY 1 LOOP
        h := encode(sha256(convert_to('["qw-journal-chain/1",'
            || app.portability_json(t::text) || ',' || app.portability_json(acct)
            || ']', 'UTF8')), 'hex');
        n := 0;
        FOR r IN SELECT e.event_id, e.recorded_at FROM app.ledger_event e
                WHERE e.tenant_id = t AND e.account_id = acct
                ORDER BY e.account_rev LOOP
            n := n + 1;
            h := encode(sha256(convert_to('["qw-journal-chain/1",'
                || app.portability_json(h) || ',' || n || ','
                || app.portability_json(r.event_id) || ','
                || app.portability_json(app.portability_instant(r.recorded_at))
                || ']', 'UTF8')), 'hex');
        END LOOP;
        out := out || jsonb_build_array(jsonb_build_object('account_id', acct,
            'revision', n + 1, 'event_count', n, 'head', h));
    END LOOP;
    RETURN out;
END
$$;

CREATE FUNCTION app.portability_import_journal(p_events jsonb, p_chains jsonb)
RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    t uuid := app.current_tenant_id();
    k bigint;
    keys text[] := ARRAY['account_id', 'event_id', 'account_rev', 'kind',
        'effective_at', 'recorded_at', 'source_id', 'source_record_id', 'reverses',
        'supersedes', 'lot_policy', 'terms', 'money', 'units'];
    e jsonb;
    r app.ledger_event;
    n integer := 0;
BEGIN
    IF t IS NULL THEN
        RAISE EXCEPTION 'portability import: no tenant' USING ERRCODE = 'QI003';
    END IF;
    k := hashtextextended('import:' || t::text, 0);
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks l WHERE l.locktype = 'advisory'
            AND l.pid = pg_backend_pid() AND l.granted AND l.mode = 'ExclusiveLock'
            AND l.database = (SELECT d.oid FROM pg_catalog.pg_database d
                              WHERE d.datname = current_database())
            AND l.classid = ((k >> 32) & 4294967295)::oid
            AND l.objid = (k & 4294967295)::oid AND l.objsubid = 1) THEN
        RAISE EXCEPTION 'portability import: the import lock is not held'
            USING ERRCODE = 'QI002';
    END IF;
    IF EXISTS (SELECT 1 FROM app.ledger_event x WHERE x.tenant_id = t)
       OR EXISTS (SELECT 1 FROM app.source_record x WHERE x.tenant_id = t) THEN
        RAISE EXCEPTION 'portability import: the journal is not empty'
            USING ERRCODE = 'QI001';
    END IF;
    -- the stamp trigger's import branch, for this definer only; a subtransaction
    -- abort undoes it, and it is cleared again below
    PERFORM set_config('app.journal_import', 'on', true);
    IF jsonb_typeof(p_events) IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_chains) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'portability import: arguments' USING ERRCODE = 'QI003';
    END IF;
    FOR e IN SELECT x FROM jsonb_array_elements(p_events) x LOOP
        IF jsonb_typeof(e) <> 'object' OR NOT e ?& keys OR e - keys <> '{}'::jsonb
           OR jsonb_typeof(e -> 'account_rev') <> 'number'
           OR jsonb_typeof(e -> 'money') <> 'array'
           OR jsonb_typeof(e -> 'units') <> 'array'
           OR app.budget_book_instant(e -> 'effective_at') IS NULL
           OR app.budget_book_instant(e -> 'recorded_at') IS NULL THEN
            RAISE EXCEPTION 'portability import: event %', n USING ERRCODE = 'QI003';
        END IF;
        INSERT INTO app.ledger_event (tenant_id, account_id, event_id, account_rev,
            kind, effective_at, recorded_at, source_id, source_record_id, reverses,
            supersedes, lot_policy, terms)
        VALUES (t, e ->> 'account_id', e ->> 'event_id',
            (e ->> 'account_rev')::bigint, (e ->> 'kind')::app.ledger_event_kind,
            app.budget_book_instant(e -> 'effective_at'),
            app.budget_book_instant(e -> 'recorded_at'), e ->> 'source_id',
            e ->> 'source_record_id', e ->> 'reverses', e ->> 'supersedes',
            e ->> 'lot_policy', e -> 'terms')
        RETURNING * INTO r;
        INSERT INTO app.posting (tenant_id, event_id, line, book_account, currency,
            wire, instrument_id)
        SELECT t, r.event_id, (o - 1)::smallint, (m ->> 0)::app.book_account,
               m ->> 1, m ->> 2, (m ->> 3)::uuid
        FROM jsonb_array_elements(e -> 'money') WITH ORDINALITY x (m, o);
        INSERT INTO app.unit_posting (tenant_id, event_id, line, unit_account,
            instrument_id, wire)
        SELECT t, r.event_id, (o - 1)::smallint, (m ->> 0)::app.unit_account,
               (m ->> 1)::uuid, m ->> 2
        FROM jsonb_array_elements(e -> 'units') WITH ORDINALITY x (m, o);
        IF app.portability_event_id(r) IS DISTINCT FROM r.event_id THEN
            RAISE EXCEPTION 'portability import: event % does not reproduce', n
                USING ERRCODE = 'QI003';
        END IF;
        n := n + 1;
    END LOOP;
    IF app.portability_chain(t) IS DISTINCT FROM p_chains THEN
        RAISE EXCEPTION 'portability import: the journal chain' USING ERRCODE = 'QI003';
    END IF;
    PERFORM set_config('app.journal_import', '', true);
    SET CONSTRAINTS app.balanced IMMEDIATE;  -- 0007's commit check, now
    SET CONSTRAINTS app.balanced DEFERRED;
    RETURN n;
END
$$;

REVOKE ALL ON FUNCTION app.portability_import_journal(jsonb, jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.portability_import_journal(jsonb, jsonb) TO qw_app;

CREATE OR REPLACE FUNCTION app.ledger_event_stamp() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    latest timestamptz;
    rev bigint;
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
    ELSE
        NEW.recorded_at := greatest(clock_timestamp(), latest);
        NEW.account_rev := rev + 1;
    END IF;
    NEW.tx_id := pg_current_xact_id();
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION app.virtual_account_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    acct text := to_jsonb(NEW) ->> CASE TG_TABLE_NAME WHEN 'simulation' THEN 'id'
                                   ELSE 'account_id' END;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(
        hashtextextended('import:' || NEW.tenant_id::text, 0));  -- before, as above
    PERFORM pg_advisory_xact_lock(
        hashtextextended('ledger:' || NEW.tenant_id || ':' || acct, 0));
    IF (TG_TABLE_NAME = 'simulation' AND EXISTS (SELECT 1 FROM app.ledger_event e
            WHERE e.tenant_id = NEW.tenant_id AND e.account_id = acct))
       OR (TG_TABLE_NAME = 'ledger_event' AND EXISTS (SELECT 1 FROM app.simulation s
            WHERE s.tenant_id = NEW.tenant_id AND s.id = acct)) THEN
        RAISE EXCEPTION 'app.%: % is both a virtual account and a real journal account',
            TG_TABLE_NAME, acct USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
