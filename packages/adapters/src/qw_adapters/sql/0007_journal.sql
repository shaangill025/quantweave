-- 0007: financial journal and source records (T012 increment 2; spec 4 "Monetary and
-- unit ledgers", "Idempotency and conflict resolution", "Corrections and retention";
-- T008 C-12, C-13, F-01, F-02, F-05). Mirrors qw_domain.postings / qw_domain.journal.
-- - ledger_event is the header; posting (money) and unit_posting (units) are separate
--   tables linked by event id, so no query adds shares to dollars.
-- - Append-only: runtime roles get SELECT and column INSERT only, and statement
--   triggers refuse any role's ordinary UPDATE, DELETE and TRUNCATE. The owner and a
--   superuser can still disable triggers or use replica mode, so those credentials
--   stay out of runtime and need audit.
-- - Lines may only be added in the transaction that inserted their event (tx_id).
--   A deferred constraint trigger checks at commit: 0 or >= 2 lines per set, at
--   least one line, money sums to 0 per currency, units to 0 per instrument; a
--   reversal is the exact negation of its original (same account and effective time,
--   not itself a reversal); a superseding event's original has a reversal.
-- - account_rev, recorded_at and tx_id are set by a trigger, never by a client, under
--   a per-account advisory lock held to commit: account_rev = latest + 1 is gapless
--   and becomes visible in assignment order, so it is the stable as-of key and the
--   replay order. recorded_at = greatest(clock_timestamp(), latest) is informational
--   knowledge time: an "as known at t" read can later gain an event stamped <= t.
-- - Amounts are inserted as canonical decimal strings (`wire`) whose CHECK enforces
--   the MoneyAmount/Quantity class (26 integer digits, scale <= 12, no trailing
--   zeros, non-zero); the numeric(38,12) column is generated from it, so a value
--   beyond scale 12 is an error, never rounded.
-- No SECURITY DEFINER function: every check runs as the inserting role under RLS.

CREATE TYPE app.ledger_event_kind AS ENUM ('deposit', 'withdrawal', 'buy', 'sell',
    'fee', 'dividend', 'interest', 'split', 'reversal');
CREATE TYPE app.book_account AS ENUM ('cash', 'security_cost', 'external_capital',
    'realized_pl', 'income', 'expense', 'transfer_clearing');
CREATE TYPE app.unit_account AS ENUM ('position', 'unit_clearing',
    'corporate_action_clearing');

-- An imported record exactly as received (string payload); not a journal entry.
CREATE TABLE app.source_record (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    account_id text NOT NULL CHECK (account_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    source_id text NOT NULL CHECK (source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    record_id text NOT NULL CHECK (record_id ~ '^[ -~]+$' AND char_length(record_id) <= 256),
    observed_at timestamptz NOT NULL,
    effective_at timestamptz NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'
        AND octet_length(payload::text) <= 65536
        AND NOT jsonb_path_exists(payload, '$.* ? (@.type() != "string")')),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, account_id, source_id, record_id)
);

CREATE TABLE app.ledger_event (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    account_id text NOT NULL CHECK (account_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    event_id text NOT NULL CHECK (event_id ~ '^[0-9a-f]{64}$'),  -- content fingerprint
    account_rev bigint NOT NULL CHECK (account_rev >= 1),
    kind app.ledger_event_kind NOT NULL,
    effective_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    tx_id xid8 NOT NULL,
    source_id text NOT NULL CHECK (source_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    source_record_id text NOT NULL
        CHECK (source_record_id ~ '^[ -~]+$' AND char_length(source_record_id) <= 256),
    reverses text,
    supersedes text,
    lot_policy text NOT NULL CHECK (lot_policy ~ '^[a-z][a-z0-9_]*/[0-9]+$'),
    -- Non-posting terms: the raw trade fee as a decimal-string Money, or nothing.
    terms jsonb NOT NULL CHECK (jsonb_typeof(terms) = 'object'
        AND (terms - 'fee') = '{}'::jsonb
        AND (NOT terms ? 'fee' OR (((terms -> 'fee') - 'amount' - 'currency') = '{}'
            AND jsonb_typeof(terms -> 'fee' -> 'amount') = 'string'
            AND (terms -> 'fee' ->> 'currency') ~ '^[A-Z]{3}$'
            AND (terms -> 'fee' ->> 'amount')
                ~ '^(0|-?[1-9][0-9]{0,25}(\.[0-9]{0,11}[1-9])?|-?0\.[0-9]{0,11}[1-9])$'))),
    -- C-03/F-05 key: one event per source record, one reversal per original. '/'
    -- cannot occur in a source id, so the encoding is unambiguous.
    idempotency_key text GENERATED ALWAYS AS (CASE WHEN reverses IS NULL
        THEN 'record/' || source_id || '/' || source_record_id
        ELSE 'reversal/' || reverses END) STORED,
    PRIMARY KEY (tenant_id, event_id),
    UNIQUE (tenant_id, account_id, event_id),
    UNIQUE (tenant_id, account_id, idempotency_key),
    UNIQUE (tenant_id, account_id, account_rev),
    CHECK ((kind = 'reversal') = (reverses IS NOT NULL)),
    CHECK (supersedes IS NULL OR reverses IS NULL),
    FOREIGN KEY (tenant_id, account_id, reverses)
        REFERENCES app.ledger_event (tenant_id, account_id, event_id),
    FOREIGN KEY (tenant_id, account_id, supersedes)
        REFERENCES app.ledger_event (tenant_id, account_id, event_id)
);
CREATE INDEX ledger_event_effective ON app.ledger_event
    (tenant_id, account_id, effective_at);

CREATE TABLE app.posting (
    tenant_id uuid NOT NULL,
    event_id text NOT NULL,
    line smallint NOT NULL CHECK (line BETWEEN 0 AND 63),
    book_account app.book_account NOT NULL,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    wire text NOT NULL
        CHECK (wire ~ '^-?(0|[1-9][0-9]{0,25})(\.[0-9]{0,11}[1-9])?$'),
    amount numeric(38, 12) GENERATED ALWAYS AS (wire::numeric(38, 12)) STORED
        CHECK (amount <> 0),
    instrument_id uuid,
    PRIMARY KEY (tenant_id, event_id, line),
    FOREIGN KEY (tenant_id, event_id) REFERENCES app.ledger_event (tenant_id, event_id),
    CHECK (book_account <> 'security_cost' OR instrument_id IS NOT NULL)
);

CREATE TABLE app.unit_posting (
    tenant_id uuid NOT NULL,
    event_id text NOT NULL,
    line smallint NOT NULL CHECK (line BETWEEN 0 AND 63),
    unit_account app.unit_account NOT NULL,
    instrument_id uuid NOT NULL,
    wire text NOT NULL
        CHECK (wire ~ '^-?(0|[1-9][0-9]{0,25})(\.[0-9]{0,11}[1-9])?$'),
    quantity numeric(38, 12) GENERATED ALWAYS AS (wire::numeric(38, 12)) STORED
        CHECK (quantity <> 0),
    PRIMARY KEY (tenant_id, event_id, line),
    FOREIGN KEY (tenant_id, event_id) REFERENCES app.ledger_event (tenant_id, event_id)
);

CREATE FUNCTION app.journal_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'app.% is append-only: % refused', TG_TABLE_NAME, TG_OP;
END
$$;

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.source_record
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.ledger_event
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.posting
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.unit_posting
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

-- Server-assigned knowledge time. The lock key matches qw_adapters.journal_store.
CREATE FUNCTION app.ledger_event_stamp() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('ledger:' || NEW.tenant_id || ':' || NEW.account_id, 0));
    SELECT greatest(clock_timestamp(), max(e.recorded_at)),
           coalesce(max(e.account_rev), 0) + 1 INTO NEW.recorded_at, NEW.account_rev
        FROM app.ledger_event e
        WHERE e.tenant_id = NEW.tenant_id AND e.account_id = NEW.account_id;
    NEW.tx_id := pg_current_xact_id();
    RETURN NEW;
END
$$;
CREATE TRIGGER stamp BEFORE INSERT ON app.ledger_event
    FOR EACH ROW EXECUTE FUNCTION app.ledger_event_stamp();

CREATE FUNCTION app.posting_same_transaction() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.ledger_event e WHERE e.tenant_id = NEW.tenant_id
            AND e.event_id = NEW.event_id AND e.tx_id = pg_current_xact_id()) THEN
        RAISE EXCEPTION 'app.%: lines are added only with their event', TG_TABLE_NAME
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER same_transaction BEFORE INSERT ON app.posting
    FOR EACH ROW EXECUTE FUNCTION app.posting_same_transaction();
CREATE TRIGGER same_transaction BEFORE INSERT ON app.unit_posting
    FOR EACH ROW EXECUTE FUNCTION app.posting_same_transaction();

CREATE FUNCTION app.ledger_event_check() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    m integer;
    u integer;
    orig app.ledger_event;
BEGIN
    SELECT count(*) INTO m FROM app.posting p
        WHERE p.tenant_id = NEW.tenant_id AND p.event_id = NEW.event_id;
    SELECT count(*) INTO u FROM app.unit_posting p
        WHERE p.tenant_id = NEW.tenant_id AND p.event_id = NEW.event_id;
    IF m = 1 OR u = 1 OR m + u = 0 THEN
        RAISE EXCEPTION 'ledger event %: each set needs 0 or >= 2 lines, one set >= 2',
            NEW.event_id USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM app.posting p WHERE p.tenant_id = NEW.tenant_id
            AND p.event_id = NEW.event_id GROUP BY p.currency HAVING sum(p.amount) <> 0)
       OR EXISTS (SELECT 1 FROM app.unit_posting p WHERE p.tenant_id = NEW.tenant_id
            AND p.event_id = NEW.event_id GROUP BY p.instrument_id
            HAVING sum(p.quantity) <> 0) THEN
        RAISE EXCEPTION 'ledger event %: unbalanced per commodity', NEW.event_id
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.reverses IS NOT NULL THEN
        SELECT * INTO orig FROM app.ledger_event e
            WHERE e.tenant_id = NEW.tenant_id AND e.event_id = NEW.reverses;
        IF orig.kind = 'reversal' OR orig.effective_at <> NEW.effective_at
           OR EXISTS ((SELECT p.book_account, p.currency, -p.amount, p.instrument_id
                   FROM app.posting p WHERE p.tenant_id = orig.tenant_id
                   AND p.event_id = orig.event_id)
               EXCEPT ALL (SELECT p.book_account, p.currency, p.amount, p.instrument_id
                   FROM app.posting p WHERE p.tenant_id = NEW.tenant_id
                   AND p.event_id = NEW.event_id))
           OR EXISTS ((SELECT p.unit_account, p.instrument_id, -p.quantity
                   FROM app.unit_posting p WHERE p.tenant_id = orig.tenant_id
                   AND p.event_id = orig.event_id)
               EXCEPT ALL (SELECT p.unit_account, p.instrument_id, p.quantity
                   FROM app.unit_posting p WHERE p.tenant_id = NEW.tenant_id
                   AND p.event_id = NEW.event_id))
           OR m <> (SELECT count(*) FROM app.posting p WHERE p.tenant_id =
                orig.tenant_id AND p.event_id = orig.event_id)
           OR u <> (SELECT count(*) FROM app.unit_posting p WHERE p.tenant_id =
                orig.tenant_id AND p.event_id = orig.event_id) THEN
            RAISE EXCEPTION 'ledger event %: not the exact reversal of %',
                NEW.event_id, NEW.reverses USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF NEW.supersedes IS NOT NULL AND NOT EXISTS (SELECT 1 FROM app.ledger_event e
            WHERE e.tenant_id = NEW.tenant_id AND e.reverses = NEW.supersedes) THEN
        RAISE EXCEPTION 'ledger event %: reverse % before superseding it',
            NEW.event_id, NEW.supersedes USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END
$$;
CREATE CONSTRAINT TRIGGER balanced AFTER INSERT ON app.ledger_event
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.ledger_event_check();

ALTER TABLE app.source_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.source_record FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.source_record
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.ledger_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.ledger_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.ledger_event
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.posting ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.posting FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.posting
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.unit_posting ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.unit_posting FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.unit_posting
    USING (tenant_id = app.current_tenant_id());

-- Imports run as qw_worker, manual entries as qw_app. No UPDATE, DELETE, TRUNCATE.
GRANT USAGE ON TYPE app.ledger_event_kind, app.book_account, app.unit_account
    TO qw_app, qw_worker;
-- qw_ingest (T008 section 5) writes source records only, never the journal.
GRANT EXECUTE ON FUNCTION app.current_tenant_id() TO qw_ingest;
GRANT SELECT, INSERT (tenant_id, account_id, source_id, record_id, observed_at,
    effective_at, payload, fingerprint) ON app.source_record
    TO qw_app, qw_worker, qw_ingest;
GRANT SELECT, INSERT (tenant_id, account_id, event_id, kind, effective_at, source_id,
    source_record_id, reverses, supersedes, lot_policy, terms)
    ON app.ledger_event TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, event_id, line, book_account, currency, wire,
    instrument_id) ON app.posting TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, event_id, line, unit_account, instrument_id, wire)
    ON app.unit_posting TO qw_app, qw_worker;
