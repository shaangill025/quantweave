-- 0004: API exposure controls (T010 increment 3): C-03 idempotency records, a
-- persistent authentication throttle, session idle tracking and the one-time
-- installation bootstrap marker. Grants go to qw_app
-- only (never PUBLIC; no DELETE or TRUNCATE; purging is later work, T025).

-- C-03: keyed by tenant, principal, method, concrete route and client key. Holds an
-- HMAC of the request and the first 2xx response, written with the effect.
CREATE TABLE app.idempotency_record (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    method text NOT NULL CHECK (method IN ('POST', 'PUT', 'PATCH', 'DELETE')),
    route text NOT NULL CHECK (char_length(route) BETWEEN 1 AND 512),
    idem_key text NOT NULL CHECK (idem_key ~ '^[A-Za-z0-9_-]{16,128}$'),
    request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
    status_code integer NOT NULL CHECK (status_code BETWEEN 200 AND 299),
    response_body bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id, method, route, idem_key),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.app_user (tenant_id, id)
);
ALTER TABLE app.idempotency_record ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.idempotency_record FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.idempotency_record
    USING (tenant_id = app.current_tenant_id());

-- Installation-scoped throttle (attempts arrive before a tenant is known). Rows hold
-- only an HMAC of (purpose, route, login or address) and counters, no tenant data,
-- so the policy admits every row to the runtime role.
CREATE TABLE app.auth_throttle (
    key_hash bytea PRIMARY KEY CHECK (octet_length(key_hash) = 32),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    window_start timestamptz NOT NULL DEFAULT now(),
    lockouts integer NOT NULL DEFAULT 0 CHECK (lockouts BETWEEN 0 AND 20),
    locked_until timestamptz
);
ALTER TABLE app.auth_throttle ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.auth_throttle FORCE ROW LEVEL SECURITY;
CREATE POLICY installation_scope ON app.auth_throttle USING (true);

-- Count one attempt atomically (the row lock serialises concurrent callers). Past
-- p_max attempts in p_window the key locks for p_base * 2^lockouts, capped at
-- p_cap. Returns the lock end (NULL when the attempt may proceed) and whether this
-- call set it.
CREATE FUNCTION app.throttle_hit(
    p_key bytea, p_max integer, p_window interval, p_base interval, p_cap interval,
    OUT locked_until timestamptz, OUT newly_locked boolean
)
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    r app.auth_throttle;
BEGIN
    INSERT INTO app.auth_throttle (key_hash) VALUES (p_key)
        ON CONFLICT (key_hash) DO NOTHING;
    SELECT * INTO STRICT r FROM app.auth_throttle t WHERE t.key_hash = p_key
        FOR UPDATE;
    newly_locked := false;
    IF r.locked_until <= now() - p_cap THEN  -- quiet for a full cap: forgive
        r.lockouts := 0;
    END IF;
    IF r.locked_until > now() THEN
        locked_until := r.locked_until;
        RETURN;
    END IF;
    IF r.window_start <= now() - p_window THEN
        r.attempts := 0;
        r.window_start := now();
    END IF;
    r.attempts := r.attempts + 1;
    IF r.attempts > p_max THEN
        r.locked_until := now() + least(p_base * (1 << r.lockouts), p_cap);
        r.lockouts := least(r.lockouts + 1, 20);
        r.attempts := 0;
        newly_locked := true;
        locked_until := r.locked_until;
    END IF;
    UPDATE app.auth_throttle t SET attempts = r.attempts,
        window_start = r.window_start, lockouts = r.lockouts,
        locked_until = r.locked_until
        WHERE t.key_hash = p_key;
END
$$;

-- Self-hosted first-owner bootstrap: at most one row ever (the PK admits only
-- true), so the operator CLI cannot run twice. Installation-scoped like the throttle.
CREATE TABLE app.installation_bootstrap (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.installation_bootstrap ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.installation_bootstrap FORCE ROW LEVEL SECURITY;
CREATE POLICY installation_scope ON app.installation_bootstrap USING (true);

-- Idle timeout: lookup moves last_used_at forward (throttled); nothing else may.
ALTER TABLE app.session ADD COLUMN last_used_at timestamptz;

CREATE OR REPLACE FUNCTION app.session_update_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.user_id, NEW.token_hash, NEW.created_at,
        NEW.expires_at) IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.user_id,
        OLD.token_hash, OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'app.session: only revoked_at, step_up_at and last_used_at may change';
    END IF;
    IF NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
        AND (OLD.revoked_at IS NOT NULL OR NEW.revoked_at <> now()) THEN
        RAISE EXCEPTION 'app.session: revoked_at is set once, to now()';
    END IF;
    IF NEW.step_up_at IS DISTINCT FROM OLD.step_up_at
        AND (NEW.step_up_at IS NULL OR NEW.step_up_at <> now()) THEN
        RAISE EXCEPTION 'app.session: step_up_at may only be set to now()';
    END IF;
    IF NEW.last_used_at IS DISTINCT FROM OLD.last_used_at
        AND (NEW.last_used_at IS NULL OR NEW.last_used_at <> now()
             OR NEW.last_used_at < OLD.last_used_at) THEN
        RAISE EXCEPTION 'app.session: last_used_at may only move forward to now()';
    END IF;
    RETURN NEW;
END
$$;

GRANT SELECT, INSERT (tenant_id, user_id, method, route, idem_key, request_hash,
    status_code, response_body) ON app.idempotency_record TO qw_app;
GRANT SELECT, INSERT (key_hash), UPDATE (attempts, window_start, lockouts,
    locked_until) ON app.auth_throttle TO qw_app;
GRANT EXECUTE ON FUNCTION app.throttle_hit(bytea, integer, interval, interval,
    interval) TO qw_app;
GRANT SELECT (last_used_at), UPDATE (last_used_at) ON app.session TO qw_app;
GRANT SELECT, INSERT (tenant_id) ON app.installation_bootstrap TO qw_app;
