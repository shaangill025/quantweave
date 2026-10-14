-- 0020: notification preferences (T056 increment 4; contract setNotificationPreferences,
-- `NotificationPreferences`; spec §13, ONB19). Adapter:
-- qw_adapters.notification_preference_store, which re-hashes every revision, checks
-- the chain and builds qw_domain.notifications.Preferences, failing closed.
-- - One append-only revision per change and user (owner too); revision = previous
--   + 1 from 1, each naming the previous revision's hash. The latest is in force;
--   no revision means the domain defaults (revision 0, in-app only).
-- - Email and browser push cannot be enabled (CHECK): whether enabling an external
--   channel needs step-up is an open owner decision, and no transport exists.
--   Detail exposure is not offered (redact_financial_values is always true).
-- - recorded_at is the DB clock (at most 60 s back, never ahead), never before the
--   previous revision; the session is the user's own live session. Inserts
--   serialise on 'notification_preference:<tenant>:<user>', taken after the
--   'risk_pause:<tenant>' lock when both are held (the listNotifications read).
-- - qw_app: SELECT and column INSERT. qw_worker: nothing yet (the producer that
--   builds recipients is remaining work).

CREATE TABLE app.notification_preference (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision BETWEEN 1 AND 2147483646),
    session_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{16,128}$'),
    in_app boolean NOT NULL,
    email boolean NOT NULL CHECK (NOT email),
    browser_push boolean NOT NULL CHECK (NOT browser_push),
    quiet_hours_timezone text NOT NULL
        CHECK (quiet_hours_timezone ~ '^[A-Za-z][A-Za-z0-9_+/-]{0,63}$'),
    quiet_hours text[] NOT NULL CHECK (cardinality(quiet_hours) <= 8
        AND array_position(quiet_hours, NULL) IS NULL
        AND (cardinality(quiet_hours) = 0 OR array_to_string(quiet_hours, ',') ~
            '^[0-9]{2}:[0-9]{2}-[0-9]{2}:[0-9]{2}(,[0-9]{2}:[0-9]{2}-[0-9]{2}:[0-9]{2})*$')),
    redact_financial_values boolean NOT NULL CHECK (redact_financial_values),
    recorded_at timestamptz NOT NULL,
    previous_hash text CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, user_id, revision),
    UNIQUE (tenant_id, user_id, idempotency_key),
    CHECK ((revision = 1) = (previous_hash IS NULL)),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.membership (tenant_id, user_id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES app.session (tenant_id, id)
);

CREATE FUNCTION app.notification_preference_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    p record;
    s record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('notification_preference:'
        || NEW.tenant_id::text || ':' || NEW.user_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.notification_preference: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT revision, recorded_at, event_hash INTO p FROM app.notification_preference
        WHERE tenant_id = NEW.tenant_id AND user_id = NEW.user_id
        ORDER BY revision DESC LIMIT 1;
    IF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at THEN
        RAISE EXCEPTION 'app.notification_preference: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT user_id, revoked_at, expires_at INTO s FROM app.session
        WHERE tenant_id = NEW.tenant_id AND id = NEW.session_id;
    IF NOT FOUND OR s.user_id <> NEW.user_id OR s.revoked_at IS NOT NULL
       OR s.expires_at <= now() THEN
        RAISE EXCEPTION 'app.notification_preference: not the user''s live session'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.notification_preference
    FOR EACH ROW EXECUTE FUNCTION app.notification_preference_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.notification_preference
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.notification_preference
    ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.notification_preference
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT, INSERT (tenant_id, user_id, revision, session_id, idempotency_key,
    in_app, email, browser_push, quiet_hours_timezone, quiet_hours,
    redact_financial_values, recorded_at, previous_hash, event_hash)
    ON app.notification_preference TO qw_app;
