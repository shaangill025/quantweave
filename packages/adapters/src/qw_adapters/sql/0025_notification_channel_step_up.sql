-- 0025: external notification channels behind step-up (T056 increment 5; owner
-- decision 4, 2026-10-15: "An external notification channel (email, push) requires
-- step-up authentication."). Extends 0020, which is not edited.
-- - The CHECKs keeping email and browser push off are dropped. A revision that
--   turns either on where the previous revision (or the defaults) had it off must
--   carry step_up_at: the inserting session's current step_up_at, at most 1 hour
--   old (the Settings maximum; the API applies its configured window), as the
--   0009/0016 guards check it. Any step_up_at given is checked the same way.
--   Keeping a channel on or turning one off needs none.
-- - Existing rows read NULL (no step-up evidence): the column has no default, so
--   no row is rewritten, the append-only triggers are not involved and their hashes
--   are unchanged (the adapter hashes step_up_at only when present).
-- - Nothing sends: no transport exists, and adding one needs owner authorisation.

ALTER TABLE app.notification_preference
    DROP CONSTRAINT notification_preference_email_check,
    DROP CONSTRAINT notification_preference_browser_push_check,
    ADD COLUMN step_up_at timestamptz,
    ADD CONSTRAINT notification_preference_step_up_before
        CHECK (step_up_at <= recorded_at);

CREATE OR REPLACE FUNCTION app.notification_preference_guard() RETURNS trigger
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
    SELECT revision, recorded_at, event_hash, email, browser_push INTO p
        FROM app.notification_preference
        WHERE tenant_id = NEW.tenant_id AND user_id = NEW.user_id
        ORDER BY revision DESC LIMIT 1;
    IF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at THEN
        RAISE EXCEPTION 'app.notification_preference: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT user_id, revoked_at, expires_at, step_up_at INTO s FROM app.session
        WHERE tenant_id = NEW.tenant_id AND id = NEW.session_id;
    IF NOT FOUND OR s.user_id <> NEW.user_id OR s.revoked_at IS NOT NULL
       OR s.expires_at <= now() THEN
        RAISE EXCEPTION 'app.notification_preference: not the user''s live session'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (NEW.step_up_at IS NULL AND ((NEW.email AND NOT coalesce(p.email, false))
            OR (NEW.browser_push AND NOT coalesce(p.browser_push, false))))
       OR (NEW.step_up_at IS NOT NULL
            AND (s.step_up_at IS DISTINCT FROM NEW.step_up_at
                 OR NEW.step_up_at < now() - interval '1 hour')) THEN
        RAISE EXCEPTION 'app.notification_preference: no valid step-up'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

GRANT INSERT (step_up_at) ON app.notification_preference TO qw_app;
