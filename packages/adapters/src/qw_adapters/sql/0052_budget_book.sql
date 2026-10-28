-- The durable budget book (T037; spec §10, §11, §15; T008 F-18; prerequisite of
-- owner decision 94). Rules and reasons: docs/receipts/T037.md, increment 3.
-- `budget_authorization`: the owner's spending authorizations per tenant; only
-- the owner writes it (`SET ROLE qw_migrate; INSERT ...`), runtime roles read.
-- `budget_book`: one book per tenant and UTC month (scope = tenant), the
-- configured caps (USD100, improvement USD20; a change is a migration) with a
-- null or owner-recorded ref (null -> ref is the one caps change), the
-- `BudgetBook` wire, its version and the store's `book_hash`; forced RLS; qw_app
-- and qw_worker read, open and update, nobody deletes. The invoker guard accepts
-- only what `qw_domain.budget` loads and a domain step makes, at the database
-- clock: exact canonical wire (`budget_book_id`, `budget_book_instant`), the
-- transitions of `budget_book_step`, one fresh authorized addition per step
-- within the caps, created in the last 60 s (`stale_clock`), deadline within
-- MAX_DEADLINE (= qw_domain.budget.MAX_DEADLINE). A reported overrun is kept
-- as reported (it may leave the book above a cap and then blocks additions).

CREATE FUNCTION app.budget_book_id(w jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog
AS $$  -- qw_domain.sources.ID_PATTERN (re.ASCII)
    SELECT coalesce(jsonb_typeof(w) = 'string'
        AND (w #>> '{}') ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'
        AND octet_length(w #>> '{}') = char_length(w #>> '{}'), false)
$$;

CREATE FUNCTION app.budget_book_instant(w jsonb) RETURNS timestamptz
LANGUAGE plpgsql STABLE SET search_path = pg_catalog
AS $$  -- `format_instant`'s form only (no zero fraction, 24:00 or :60), else NULL
DECLARE
    t text := w #>> '{}'; ts timestamp; back text;
BEGIN
    IF coalesce(jsonb_typeof(w) <> 'string' OR t !~
       '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?Z$', true)
    THEN RETURN NULL; END IF;
    ts := left(t, -1)::timestamp;
    back := to_char(ts, 'YYYY-MM-DD"T"HH24:MI:SS')
        || CASE WHEN date_trunc('second', ts) = ts THEN '' ELSE to_char(ts, '.US') END;
    RETURN CASE WHEN back || 'Z' = t THEN ts AT TIME ZONE 'UTC' END;
EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
    RETURN NULL;
END
$$;

CREATE TABLE app.budget_authorization (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    authorization_ref text NOT NULL
        CHECK (app.budget_book_id(to_jsonb(authorization_ref))),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, authorization_ref)
);

CREATE TABLE app.budget_book (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    scope_id text NOT NULL,
    period text NOT NULL CHECK (period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    version bigint NOT NULL CHECK (version >= 0),
    book jsonb NOT NULL,
    book_hash text NOT NULL CHECK (book_hash ~ '^[0-9a-f]{64}$'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, period),
    CONSTRAINT budget_book_scope CHECK (scope_id = tenant_id::text),
    CONSTRAINT budget_book_wire CHECK ((jsonb_typeof(book) = 'object'
        AND book - ARRAY['scope_id', 'period', 'caps', 'reservations', 'version']
            = '{}'::jsonb
        AND book ->> 'scope_id' = scope_id AND book ->> 'period' = period
        AND book -> 'version' = to_jsonb(version)
        AND jsonb_typeof(book -> 'reservations') = 'array') IS TRUE),
    CONSTRAINT budget_book_caps CHECK ((jsonb_typeof(book -> 'caps') = 'object'
        AND (book -> 'caps') - ARRAY['inference', 'improvement', 'authorization_ref']
            = '{}'::jsonb
        AND book -> 'caps' -> 'inference' = '"100"'::jsonb
        AND book -> 'caps' -> 'improvement' = '"20"'::jsonb
        AND (book -> 'caps') ? 'authorization_ref') IS TRUE)
);

-- Whether one reservation's change o -> n is a domain step at `now`.
CREATE FUNCTION app.budget_book_step(o jsonb, n jsonb, now timestamptz,
                                     authorized boolean) RETURNS boolean
LANGUAGE plpgsql STABLE SET search_path = pg_catalog
AS $$
DECLARE
    os text := o ->> 'status'; ns text := n ->> 'status';
    od boolean := (o -> 'dispatched')::boolean;
    nd boolean := (n -> 'dispatched')::boolean;
    s0 bigint := (o -> 'sends')::bigint; s1 bigint := (n -> 'sends')::bigint;
    r0 bigint := (o -> 'returned')::bigint; r1 bigint := (n -> 'returned')::bigint;
    open_send boolean := now < app.budget_book_instant(n -> 'deadline') AND authorized;
    same boolean := od = nd AND s0 = s1 AND r0 = r1;
BEGIN
    IF n - ARRAY['status', 'dispatched', 'actual', 'sends', 'returned']
       <> o - ARRAY['status', 'dispatched', 'actual', 'sends', 'returned'] THEN
        RETURN false;  -- id, category, amount, account, bound, created, deadline
    END IF;
    IF n = o THEN RETURN true; END IF;
    RETURN coalesce(CASE
        WHEN os = 'reserved' AND ns = 'reserved' THEN n -> 'actual' = 'null'::jsonb
            AND (  -- dispatched
                (NOT od AND nd AND s1 = s0 AND r1 = r0 AND open_send)
                -- begin_send: none in flight
                OR (nd AND s1 = s0 + 1 AND r1 = r0 AND r0 = s0 AND open_send)
                -- end_send
                OR (od = nd AND s1 = s0 AND r1 = r0 + 1 AND r1 <= s1))
        WHEN os = 'reserved' AND ns = 'released' THEN NOT od AND same
            AND s1 = 0 AND n -> 'actual' = 'null'::jsonb
        WHEN os = 'reserved' AND ns = 'expired' THEN od AND same
            AND now >= app.budget_book_instant(n -> 'deadline')
            AND n -> 'actual' = n -> 'reserved'
        WHEN os IN ('reserved', 'expired') AND ns = 'settled' THEN od AND same
            AND n -> 'actual' <> 'null'::jsonb
        ELSE false
    END, false);
END
$$;

CREATE FUNCTION app.budget_book_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    max_deadline CONSTANT interval := interval '7 days';  -- MAX_DEADLINE
    keys CONSTANT text[] := ARRAY['reservation_id', 'scope_id', 'period',
        'category', 'reserved', 'provider_account_ref', 'max_output_tokens',
        'created_at', 'deadline', 'status', 'dispatched', 'actual', 'sends',
        'returned'];
    usd CONSTANT text := '^(0|[1-9][0-9]{0,13})(\.[0-9]{0,5}[1-9])?$';
    count_ CONSTANT text := '^(0|[1-9][0-9]{0,8})$';
    now CONSTANT timestamptz := clock_timestamp();
    ref jsonb := NEW.book -> 'caps' -> 'authorization_ref';
    cap numeric := (NEW.book -> 'caps' ->> 'inference')::numeric;
    authorized boolean; n jsonb; o jsonb; created timestamptz; charge numeric;
    added int := 0; matched int := 0; total numeric := 0; improvement numeric := 0;
BEGIN
    NEW.updated_at := now;
    authorized := EXISTS (SELECT 1 FROM app.budget_authorization a
        WHERE a.tenant_id = NEW.tenant_id AND to_jsonb(a.authorization_ref) = ref);
    IF TG_OP = 'INSERT' THEN  -- a ref is checked when set; a revoked one then
        IF NOT (authorized OR ref = 'null'::jsonb) THEN  -- blocks only new spend
            RAISE check_violation USING MESSAGE = 'app.budget_book: authorization';
        END IF;
        IF NEW.version <> 0 OR NEW.book -> 'reservations' <> '[]'::jsonb
           OR NEW.period <> to_char(now AT TIME ZONE 'UTC', 'YYYY-MM') THEN
            RAISE check_violation USING MESSAGE = 'app.budget_book: not_opened';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.tenant_id, NEW.scope_id, NEW.period)
           IS DISTINCT FROM (OLD.tenant_id, OLD.scope_id, OLD.period)
       OR NEW.version <> OLD.version + 1
       OR (NEW.book -> 'caps' IS DISTINCT FROM OLD.book -> 'caps' AND NOT (
           OLD.book -> 'caps' -> 'authorization_ref' = 'null'::jsonb AND authorized
           AND (NEW.book -> 'caps') - 'authorization_ref'
               = (OLD.book -> 'caps') - 'authorization_ref'
           AND NEW.book -> 'reservations' = OLD.book -> 'reservations')) THEN
        RAISE check_violation USING MESSAGE = 'app.budget_book: version';
    END IF;
    IF (SELECT count(*) <> count(DISTINCT x -> 'reservation_id')
        FROM jsonb_array_elements(NEW.book -> 'reservations') x) THEN
        RAISE check_violation USING MESSAGE = 'app.budget_book: duplicate';
    END IF;
    FOR n IN SELECT x FROM jsonb_array_elements(NEW.book -> 'reservations') x LOOP
        IF NOT coalesce(jsonb_typeof(n) = 'object' AND n ?& keys
            AND n - keys = '{}'::jsonb
            AND n -> 'scope_id' = to_jsonb(NEW.scope_id)
            AND n -> 'period' = to_jsonb(NEW.period)
            AND app.budget_book_id(n -> 'reservation_id')
            AND app.budget_book_id(n -> 'provider_account_ref')
            AND n -> 'category' IN ('"monitoring"', '"verification"', '"research"',
                                    '"improvement"')
            AND n -> 'status' IN ('"reserved"', '"settled"', '"released"',
                                  '"expired"')
            AND jsonb_typeof(n -> 'reserved') = 'string' AND n ->> 'reserved' ~ usd
            AND (n ->> 'reserved')::numeric > 0
            AND (n -> 'actual' = 'null'::jsonb OR (jsonb_typeof(n -> 'actual')
                 = 'string' AND n ->> 'actual' ~ usd))
            AND (n -> 'actual' = 'null'::jsonb) = (n ->> 'status' IN ('reserved',
                                                                     'released'))
            AND jsonb_typeof(n -> 'dispatched') = 'boolean'
            AND jsonb_typeof(n -> 'max_output_tokens') = 'number'
            AND n ->> 'max_output_tokens' ~ count_ AND n ->> 'max_output_tokens' <> '0'
            AND jsonb_typeof(n -> 'sends') = 'number' AND n ->> 'sends' ~ count_
            AND jsonb_typeof(n -> 'returned') = 'number' AND n ->> 'returned' ~ count_
            AND app.budget_book_instant(n -> 'created_at') IS NOT NULL
            AND app.budget_book_instant(n -> 'deadline') IS NOT NULL, false) THEN
            RAISE check_violation USING MESSAGE = 'app.budget_book: reservation_wire';
        END IF;
        charge := CASE WHEN n ->> 'status' = 'reserved'
            THEN (n ->> 'reserved')::numeric
            ELSE coalesce((n ->> 'actual')::numeric, 0) END;
        total := total + charge;
        IF n ->> 'category' = 'improvement' THEN
            improvement := improvement + charge;
        END IF;
        o := NULL;
        SELECT x INTO o FROM jsonb_array_elements(OLD.book -> 'reservations') x
            WHERE x -> 'reservation_id' = n -> 'reservation_id';
        IF o IS NOT NULL THEN
            matched := matched + 1;
            IF NOT app.budget_book_step(o, n, now, authorized) THEN
                RAISE check_violation USING MESSAGE = 'app.budget_book: transition '
                    || (n ->> 'reservation_id');
            END IF;
            CONTINUE;
        END IF;
        added := added + 1;
        created := app.budget_book_instant(n -> 'created_at');
        IF NOT (n -> 'status' = '"reserved"'::jsonb
                AND n -> 'dispatched' = 'false'::jsonb
                AND n -> 'sends' = '0'::jsonb AND n -> 'returned' = '0'::jsonb
                AND authorized AND created <= now
                AND to_char(created AT TIME ZONE 'UTC', 'YYYY-MM') = NEW.period
                AND app.budget_book_instant(n -> 'deadline') > created
                AND app.budget_book_instant(n -> 'deadline')
                    <= created + max_deadline) THEN
            RAISE check_violation USING MESSAGE = 'app.budget_book: reservation';
        END IF;
        IF created < now - interval '60 seconds' THEN
            RAISE check_violation USING MESSAGE = 'app.budget_book: stale_clock';
        END IF;
    END LOOP;
    IF matched <> jsonb_array_length(OLD.book -> 'reservations') THEN
        RAISE check_violation USING MESSAGE = 'app.budget_book: history';
    END IF;
    IF added > 1 OR (added = 1 AND (total > cap
           OR improvement > (NEW.book -> 'caps' ->> 'improvement')::numeric)) THEN
        RAISE check_violation USING MESSAGE = 'app.budget_book: cap';
    END IF;
    RETURN NEW;
END
$$;

REVOKE ALL ON FUNCTION app.budget_book_guard(), app.budget_book_id(jsonb),
    app.budget_book_instant(jsonb),
    app.budget_book_step(jsonb, jsonb, timestamptz, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.budget_book_id(jsonb), app.budget_book_instant(jsonb),
    app.budget_book_step(jsonb, jsonb, timestamptz, boolean)
    TO qw_app, qw_worker;  -- the invoker guard calls them
CREATE TRIGGER write_guard BEFORE INSERT OR UPDATE ON app.budget_book
    FOR EACH ROW EXECUTE FUNCTION app.budget_book_guard();
CREATE TRIGGER no_delete BEFORE DELETE OR TRUNCATE ON app.budget_book
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
ALTER TABLE app.budget_book ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.budget_book
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.budget_authorization ENABLE ROW LEVEL SECURITY,
    FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_read ON app.budget_authorization FOR SELECT
    USING (tenant_id = app.current_tenant_id());
CREATE POLICY owner_write ON app.budget_authorization TO qw_migrate
    USING (true) WITH CHECK (true);

GRANT SELECT ON app.budget_book, app.budget_authorization TO qw_app, qw_worker;
GRANT INSERT (tenant_id, scope_id, period, version, book, book_hash)
    ON app.budget_book TO qw_app, qw_worker;
GRANT UPDATE (version, book, book_hash) ON app.budget_book TO qw_app, qw_worker;
