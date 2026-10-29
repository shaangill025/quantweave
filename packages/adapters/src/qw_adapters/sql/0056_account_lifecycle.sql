-- Account display text and lifecycle (T013 increment 3; owner decisions 101, 102
-- and 103; contract Account `broker`, `account_type`, `revision`, `status`).
-- - Decisions 102 and 103: `broker` and `account_type` are bounded display text,
--   exactly qw_domain.sources.check_display_text (app.account_display_text below).
--   0055's CHECKs ([!-~]{1,128}) are replaced. A value
--   of 81 to 128 characters with no space was valid under 0055 and is not now;
--   ADD CONSTRAINT validates every stored row, so this migration fails rather than
--   leave such a row. Hashes are unaffected: content_hash digests the column
--   values, and row_hash seals content_hash with recorded_at; neither reads a CHECK.
-- - Decision 101: only the lifecycle is stored. Revision 1 is the app.account row
--   (active). `account_revision` appends revision n + 1, naming the previous row
--   hash (revision 1: the account's row_hash). The only lifecycle change is active
--   to archived, once; nothing unarchives (no spec text asks for it, and a new
--   account keeps the history clear). `conflicted` and cash basis are derived at
--   read time from a consolidation (qw_adapters.account_store.read_accounts).
-- - The 0055 guard is replaced so the fourth table shares its lock, stamp, seal and
--   latest-row floor (one knowledge order per tenant); the mapping checks are
--   unchanged. qw_app: SELECT and column INSERT (not recorded_at or row_hash).
--   qw_worker: SELECT. Nobody updates or deletes.

-- qw_domain.sources.check_display_text (decisions 102 and 103): already NFC; words
-- of printable ASCII and the Latin letters U+00C0-U+00D6, U+00D8-U+00F6 and
-- U+00F8-U+017F (DISPLAY_LETTERS), one U+0020 between words, 1 to 80 characters.
-- The class is built with chr() so this file stays ASCII. Bracket ranges in
-- PostgreSQL's regex engine compare code points; COLLATE "C" pins every other
-- locale-dependent behaviour whatever the database collation. NFC needs UTF8.
CREATE FUNCTION app.account_display_text(x text) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT SET search_path = pg_catalog
AS $$
    SELECT x IS NFC NORMALIZED AND char_length(x) BETWEEN 1 AND 80
        AND x COLLATE "C" ~ replace('^W+( W+)*$', 'W', '[!-~' || chr(192) || '-'
            || chr(214) || chr(216) || '-' || chr(246) || chr(248) || '-'
            || chr(383) || ']')
$$;
REVOKE ALL ON FUNCTION app.account_display_text(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.account_display_text(text) TO qw_app, qw_worker;

ALTER TABLE app.account
    DROP CONSTRAINT account_broker_check,
    DROP CONSTRAINT account_account_type_check,
    ADD CONSTRAINT account_broker_check CHECK (app.account_display_text(broker)),
    ADD CONSTRAINT account_account_type_check
        CHECK (app.account_display_text(account_type));

CREATE TABLE app.account_revision (
    tenant_id uuid NOT NULL,
    account_id text NOT NULL,
    revision integer NOT NULL CHECK (revision BETWEEN 2 AND 2147483646),
    lifecycle text NOT NULL CHECK (lifecycle = 'archived'),
    recorded_at timestamptz NOT NULL,
    previous_hash text NOT NULL CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    row_hash text NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, account_id, revision),
    FOREIGN KEY (tenant_id, account_id) REFERENCES app.account (tenant_id, account_id)
);

CREATE OR REPLACE FUNCTION app.source_map_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    latest timestamptz;
    p record;
    bad boolean;
    prev_rev integer;
    prev_hash text;
    prev_life text;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'source_map:' || NEW.tenant_id::text, 0));
    SELECT max(t) INTO latest FROM (
        SELECT max(recorded_at) t FROM app.account WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.data_source
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.source_mapping_revision
            WHERE tenant_id = NEW.tenant_id
        UNION ALL SELECT max(recorded_at) FROM app.account_revision
            WHERE tenant_id = NEW.tenant_id) x;
    NEW.recorded_at := greatest(clock_timestamp(), latest);  -- any value given
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

REVOKE ALL ON FUNCTION app.source_map_guard() FROM PUBLIC;
CREATE TRIGGER insert_guard BEFORE INSERT ON app.account_revision
    FOR EACH ROW EXECUTE FUNCTION app.source_map_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.account_revision
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.account_revision ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.account_revision
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.account_revision TO qw_app, qw_worker;
GRANT INSERT (tenant_id, account_id, revision, lifecycle, previous_hash,
    content_hash) ON app.account_revision TO qw_app;
