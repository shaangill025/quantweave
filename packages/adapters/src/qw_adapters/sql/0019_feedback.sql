-- 0019: private feedback, consent, revocations and tombstones (T043 inc. 2; spec
-- §11, §12; R031, R052), replayed by qw_adapters.feedback_store (fail closed).
-- Append-only under forced RLS; a feedback_content row (subject, category, message,
-- salt) may be DELETEd only after its tombstone (the 0012 pattern), and a record
-- has content xor a tombstone at commit. Only the original principal revokes or
-- tombstones; never covers_prior. Times: DB clock (60 s back, never ahead), in
-- tenant order, under lock 'feedback:<tenant>'. qw_app only. Retention classes
-- (T058): 'improvement', tombstones 'privacy'; no period configured.

CREATE TABLE app.feedback (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    record_id text NOT NULL CHECK (record_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    principal_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{16,128}$'),
    recorded_at timestamptz NOT NULL,
    record_hash text NOT NULL CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    retention_class text NOT NULL DEFAULT 'improvement'
        CHECK (retention_class = 'improvement'),
    PRIMARY KEY (tenant_id, record_id),
    UNIQUE (tenant_id, principal_id, idempotency_key),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES app.membership (tenant_id, user_id)
);

CREATE TABLE app.feedback_content (
    tenant_id uuid NOT NULL,
    record_id text NOT NULL,
    subject_kind text NOT NULL CHECK (subject_kind IN ('thesis', 'review', 'proposal',
        'notification', 'claim', 'model_run', 'simulation')),
    subject_id text NOT NULL CHECK (subject_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    subject_version integer NOT NULL CHECK (subject_version >= 1),
    subject_hash text NOT NULL CHECK (subject_hash ~ '^[0-9a-f]{64}$'),
    category text NOT NULL CHECK (category IN ('incorrect_fact', 'irrelevant', 'clear',
        'unclear', 'missed_risk', 'other')),
    message text NOT NULL CHECK (char_length(message) <= 4000),
    occurred_at timestamptz NOT NULL,
    salt text NOT NULL CHECK (salt ~ '^[0-9a-f]{64}$'),  -- of record_hash; deleted too
    PRIMARY KEY (tenant_id, record_id),
    FOREIGN KEY (tenant_id, record_id) REFERENCES app.feedback (tenant_id, record_id)
);

CREATE TABLE app.feedback_consent (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    consent_id text NOT NULL CHECK (consent_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    principal_id uuid NOT NULL,
    purpose text NOT NULL CHECK (purpose = 'shared_improvement'),
    recipient text NOT NULL CHECK (recipient ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    data_categories text[] NOT NULL CHECK (cardinality(data_categories) >= 1
        AND data_categories <@ ARRAY['feedback', 'error', 'outcome', 'evaluation_case']),
    terms_version text NOT NULL
        CHECK (terms_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    granted_at timestamptz NOT NULL,
    expires_at timestamptz CHECK (expires_at > granted_at),
    covers_prior boolean NOT NULL DEFAULT false CHECK (NOT covers_prior),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, consent_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES app.membership (tenant_id, user_id)
);

CREATE TABLE app.feedback_consent_revocation (
    tenant_id uuid NOT NULL,
    consent_id text NOT NULL,
    principal_id uuid NOT NULL,
    revoked_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, consent_id),
    FOREIGN KEY (tenant_id, consent_id)
        REFERENCES app.feedback_consent (tenant_id, consent_id)
);

CREATE TABLE app.feedback_tombstone (
    tenant_id uuid NOT NULL,
    record_id text NOT NULL,
    principal_id uuid NOT NULL,
    deleted_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    retention_class text NOT NULL DEFAULT 'privacy' CHECK (retention_class = 'privacy'),
    PRIMARY KEY (tenant_id, record_id),
    FOREIGN KEY (tenant_id, record_id) REFERENCES app.feedback (tenant_id, record_id)
);

CREATE FUNCTION app.feedback_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    at timestamptz := (to_jsonb(NEW) ->> TG_ARGV[0])::timestamptz;
    last timestamptz;
    owner uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'feedback:' || NEW.tenant_id::text, 0));
    IF at > clock_timestamp() OR at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock', TG_TABLE_NAME, at
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT greatest(
        (SELECT max(recorded_at) FROM app.feedback WHERE tenant_id = NEW.tenant_id),
        (SELECT max(granted_at) FROM app.feedback_consent
            WHERE tenant_id = NEW.tenant_id),
        (SELECT max(revoked_at) FROM app.feedback_consent_revocation
            WHERE tenant_id = NEW.tenant_id),
        (SELECT max(deleted_at) FROM app.feedback_tombstone
            WHERE tenant_id = NEW.tenant_id)) INTO last;
    IF at < last THEN
        RAISE EXCEPTION 'app.%: % is before the previous feedback event',
            TG_TABLE_NAME, at USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'feedback_consent_revocation' THEN
        SELECT principal_id INTO owner FROM app.feedback_consent
            WHERE tenant_id = NEW.tenant_id AND consent_id = NEW.consent_id;
    ELSIF TG_TABLE_NAME = 'feedback_tombstone' THEN
        SELECT principal_id INTO owner FROM app.feedback
            WHERE tenant_id = NEW.tenant_id AND record_id = NEW.record_id;
    ELSE
        RETURN NEW;
    END IF;
    IF owner IS DISTINCT FROM (to_jsonb(NEW) ->> 'principal_id')::uuid THEN
        RAISE EXCEPTION 'app.%: only its own principal may do this', TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

-- Content: inserted only with its record (same transaction), never after a
-- tombstone, dated no later than the record; removed only after a tombstone.
CREATE FUNCTION app.feedback_content_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    r record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF EXISTS (SELECT 1 FROM app.feedback_tombstone t
                   WHERE t.tenant_id = OLD.tenant_id AND t.record_id = OLD.record_id) THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'app.feedback_content: % has no tombstone', OLD.record_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT f.recorded_at, f.xmin = pg_current_xact_id()::xid AS fresh INTO r
        FROM app.feedback f
        WHERE f.tenant_id = NEW.tenant_id AND f.record_id = NEW.record_id;
    IF NOT FOUND OR NOT r.fresh OR NEW.occurred_at > r.recorded_at
       OR EXISTS (SELECT 1 FROM app.feedback_tombstone t
                  WHERE t.tenant_id = NEW.tenant_id AND t.record_id = NEW.record_id) THEN
        RAISE EXCEPTION 'app.feedback_content: % is not a new record', NEW.record_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

-- At commit: a record has content xor a tombstone.
CREATE FUNCTION app.feedback_settled() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    held boolean := EXISTS (SELECT 1 FROM app.feedback_content c
        WHERE c.tenant_id = NEW.tenant_id AND c.record_id = NEW.record_id);
    gone boolean := EXISTS (SELECT 1 FROM app.feedback_tombstone t
        WHERE t.tenant_id = NEW.tenant_id AND t.record_id = NEW.record_id);
BEGIN
    IF held = gone THEN
        RAISE EXCEPTION 'app.%: % content and tombstone disagree', TG_TABLE_NAME,
            NEW.record_id USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER content_guard BEFORE INSERT OR DELETE ON app.feedback_content
    FOR EACH ROW EXECUTE FUNCTION app.feedback_content_guard();
CREATE CONSTRAINT TRIGGER settled AFTER INSERT ON app.feedback DEFERRABLE
    INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.feedback_settled();
CREATE CONSTRAINT TRIGGER settled AFTER INSERT ON app.feedback_tombstone DEFERRABLE
    INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.feedback_settled();

CREATE TRIGGER append_only BEFORE UPDATE OR TRUNCATE ON app.feedback_content
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['feedback', 'feedback_content', 'feedback_consent',
                             'feedback_consent_revocation', 'feedback_tombstone'] LOOP
        IF t <> 'feedback_content' THEN
            EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR '
                'TRUNCATE ON app.%I FOR EACH STATEMENT EXECUTE FUNCTION '
                'app.journal_append_only()', t);
            EXECUTE format('CREATE TRIGGER insert_guard BEFORE INSERT ON app.%I FOR '
                'EACH ROW EXECUTE FUNCTION app.feedback_guard(%L)', t, CASE t
                WHEN 'feedback' THEN 'recorded_at' WHEN 'feedback_consent'
                THEN 'granted_at' WHEN 'feedback_tombstone' THEN 'deleted_at'
                ELSE 'revoked_at' END);
        END IF;
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY, '
            'FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON app.%I '
            'USING (tenant_id = app.current_tenant_id())', t);
    END LOOP;
END
$$;

GRANT SELECT, INSERT (tenant_id, record_id, principal_id, idempotency_key,
    recorded_at, record_hash) ON app.feedback TO qw_app;
GRANT SELECT, INSERT (tenant_id, record_id, subject_kind, subject_id, subject_version,
    subject_hash, category, message, occurred_at, salt), DELETE
    ON app.feedback_content TO qw_app;
GRANT SELECT, INSERT (tenant_id, consent_id, principal_id, purpose, recipient,
    data_categories, terms_version, granted_at, expires_at, event_hash)
    ON app.feedback_consent TO qw_app;
GRANT SELECT, INSERT (tenant_id, consent_id, principal_id, revoked_at, event_hash)
    ON app.feedback_consent_revocation TO qw_app;
GRANT SELECT, INSERT (tenant_id, record_id, principal_id, deleted_at, event_hash)
    ON app.feedback_tombstone TO qw_app;
