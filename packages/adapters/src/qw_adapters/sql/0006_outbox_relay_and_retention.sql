-- 0006: outbox relay, retention purges and job deadlines (T025 increment 2; spec 03
-- "Durable coordination"; T008 C-03, C-17, A-05, F-19). Delivery stays
-- at-least-once: the relay marks an event published only after the publish call
-- returned, in the same transaction that locked it, so a crash in between
-- republishes it and consumers deduplicate through app.inbox.
-- Cross-tenant work goes through SECURITY DEFINER functions with fixed SQL (owner
-- qw_migrate, search_path pg_catalog, EXECUTE qw_worker only) that return ids or
-- counts. Purges delete only through owner DELETE policies whose fixed predicates
-- bound what even the owner can remove; runtime roles get no DELETE grant.

-- Outbox relay state. attempts counts failed publishes; at the relay's limit the
-- event is parked (kept and visible, never dropped). Published and parked events
-- are final; an operator re-drive of parked events is later work.
ALTER TABLE app.outbox ADD COLUMN parked_at timestamptz,
    ADD CONSTRAINT outbox_one_outcome CHECK (published_at IS NULL OR parked_at IS NULL);
CREATE INDEX outbox_relay ON app.outbox (created_at, id)
    WHERE published_at IS NULL AND parked_at IS NULL;

CREATE FUNCTION app.outbox_update_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.event_type, NEW.payload, NEW.created_at)
        IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.event_type, OLD.payload, OLD.created_at) THEN
        RAISE EXCEPTION 'app.outbox: identity, payload and created_at are immutable';
    END IF;
    IF OLD.published_at IS NOT NULL OR OLD.parked_at IS NOT NULL THEN
        RAISE EXCEPTION 'app.outbox: published and parked events are final';
    END IF;
    IF NEW.attempts < OLD.attempts THEN
        RAISE EXCEPTION 'app.outbox: attempts never decrease';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER outbox_update_guard BEFORE UPDATE ON app.outbox
    FOR EACH ROW EXECUTE FUNCTION app.outbox_update_guard();

-- Owner read and lock for the relay (WITH CHECK false: writes need the tenant
-- context), and the fixed purge predicate: published more than a day ago.
CREATE POLICY relay ON app.outbox FOR SELECT TO qw_migrate USING (true);
CREATE POLICY relay_lock ON app.outbox FOR UPDATE TO qw_migrate
    USING (true) WITH CHECK (false);
CREATE POLICY purge ON app.outbox FOR DELETE TO qw_migrate
    USING (published_at < now() - interval '1 day');

-- Inbox rows are deleted only when old and when the event can no longer be
-- redelivered from this database: no unpublished or parked outbox row and no job
-- that can still run (queued, running, or dead_letter awaiting a requeue) has that
-- id. HAZARD: an inbox row is the only memory that an effect happened; a
-- redelivery from outside (a transport, a replay) after its purge runs the effect
-- again. The window must exceed the maximum redelivery horizon.
CREATE POLICY purge_read ON app.inbox FOR SELECT TO qw_migrate USING (true);
CREATE POLICY purge ON app.inbox FOR DELETE TO qw_migrate
    USING (processed_at < now() - interval '7 days'
        AND NOT EXISTS (SELECT 1 FROM app.outbox o WHERE o.tenant_id = inbox.tenant_id
            AND o.id = inbox.event_id AND o.published_at IS NULL)
        AND NOT EXISTS (SELECT 1 FROM app.job j WHERE j.tenant_id = inbox.tenant_id
            AND j.id = inbox.event_id
            AND j.state IN ('queued', 'running', 'dead_letter')));

-- HTTP idempotency records (C-03) are kept 24 h (A-05).
CREATE POLICY purge_read ON app.idempotency_record FOR SELECT TO qw_migrate
    USING (created_at < now() - interval '24 hours');
CREATE POLICY purge ON app.idempotency_record FOR DELETE TO qw_migrate
    USING (created_at < now() - interval '24 hours');

-- The oldest due, unpublished, unparked events, locked for the caller's
-- transaction; concurrent relays skip each other's rows.
CREATE FUNCTION app.outbox_pick(p_limit integer)
RETURNS TABLE (tenant_id uuid, id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT o.tenant_id, o.id FROM app.outbox o
      WHERE o.published_at IS NULL AND o.parked_at IS NULL
        AND o.next_attempt_at <= now()
      ORDER BY o.created_at, o.id LIMIT p_limit FOR UPDATE SKIP LOCKED $$;

-- Retention purges. Each refuses a window below its minimum (which the DELETE
-- policy also enforces) and returns the number of rows removed.
CREATE FUNCTION app.outbox_purge(p_older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    n integer;
BEGIN
    IF p_older_than IS NULL OR p_older_than < interval '1 day' THEN
        RAISE EXCEPTION 'outbox_purge: retention must be at least 1 day';
    END IF;
    DELETE FROM app.outbox o WHERE o.published_at < now() - p_older_than;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$$;

CREATE FUNCTION app.inbox_purge(p_older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    n integer;
BEGIN
    IF p_older_than IS NULL OR p_older_than < interval '7 days' THEN
        RAISE EXCEPTION 'inbox_purge: retention must be at least 7 days';
    END IF;
    DELETE FROM app.inbox i WHERE i.processed_at < now() - p_older_than;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$$;

CREATE FUNCTION app.idempotency_purge(p_older_than interval) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    n integer;
BEGIN
    IF p_older_than IS NULL OR p_older_than < interval '24 hours' THEN
        RAISE EXCEPTION 'idempotency_purge: retention must be at least 24 hours';
    END IF;
    DELETE FROM app.idempotency_record r WHERE r.created_at < now() - p_older_than;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$$;
-- app.installation_bootstrap is not purged: its single row is what refuses a
-- second bootstrap, and it never expires.

-- Job deadlines: a start-by time fixed at enqueue. No attempt starts after it;
-- overdue queued jobs move to expired.
ALTER TABLE app.job ADD COLUMN deadline_at timestamptz,
    ADD CONSTRAINT job_deadline_after_creation CHECK (deadline_at > created_at);
CREATE INDEX job_deadlines ON app.job (deadline_at) WHERE state = 'queued';

CREATE OR REPLACE FUNCTION app.job_update_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.kind, NEW.input_revision, NEW.priority, NEW.pool,
        NEW.payload, NEW.max_attempts, NEW.created_at, NEW.available_at,
        NEW.deadline_at)
        IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.kind, OLD.input_revision, OLD.priority, OLD.pool,
        OLD.payload, OLD.max_attempts, OLD.created_at, OLD.available_at,
        OLD.deadline_at) THEN
        RAISE EXCEPTION 'app.job: identity, input, limits and trigger time are immutable';
    END IF;
    IF OLD.state = 'dead_letter' AND NEW.state = 'queued' THEN
        IF (NEW.generation, NEW.attempts, NEW.total_attempts)
            IS DISTINCT FROM (OLD.generation + 1, 0, OLD.total_attempts) THEN
            RAISE EXCEPTION 'app.job: attempt history: requeue is generation + 1';
        END IF;
    ELSIF NEW.generation <> OLD.generation OR NEW.attempts < OLD.attempts
        OR NEW.total_attempts - OLD.total_attempts <> NEW.attempts - OLD.attempts THEN
        RAISE EXCEPTION 'app.job: attempt history only grows with attempts';
    END IF;
    IF NEW.state <> OLD.state AND (OLD.state::text, NEW.state::text) NOT IN (
        ('queued', 'running'), ('queued', 'cancelled'), ('queued', 'expired'),
        ('running', 'succeeded'), ('running', 'failed'), ('running', 'queued'),
        ('running', 'dead_letter'), ('dead_letter', 'queued')) THEN
        RAISE EXCEPTION 'app.job: illegal transition % -> %', OLD.state, NEW.state;
    END IF;
    RETURN NEW;
END
$$;

-- As 0005, plus: never a job whose deadline has passed.
CREATE OR REPLACE FUNCTION app.job_pick(p_pool app.job_pool, OUT tenant_id uuid,
    OUT id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT q.tenant_id, q.priority FROM (
            SELECT DISTINCT j.tenant_id, j.priority FROM app.job j
            WHERE j.pool = p_pool AND j.state = 'queued'
              AND j.next_available_at <= now()
              AND (j.deadline_at IS NULL OR j.deadline_at > now())) q
        ORDER BY q.priority, (SELECT max(s.claimed_at) FROM app.job s
            WHERE s.pool = p_pool AND s.priority = q.priority
              AND s.tenant_id = q.tenant_id) NULLS FIRST, q.tenant_id
    LOOP
        SELECT j.tenant_id, j.id INTO tenant_id, id FROM app.job j
            WHERE j.tenant_id = r.tenant_id AND j.pool = p_pool
              AND j.priority = r.priority AND j.state = 'queued'
              AND j.next_available_at <= now()
              AND (j.deadline_at IS NULL OR j.deadline_at > now())
            ORDER BY j.next_available_at, j.created_at, j.id
            LIMIT 1 FOR UPDATE SKIP LOCKED;
        IF FOUND THEN
            RETURN;
        END IF;
    END LOOP;
END
$$;

-- Queued jobs past their deadline, locked for the caller's transaction.
CREATE FUNCTION app.job_overdue(p_limit integer)
RETURNS TABLE (tenant_id uuid, id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT j.tenant_id, j.id FROM app.job j
      WHERE j.state = 'queued' AND j.deadline_at <= now()
      ORDER BY j.deadline_at, j.id LIMIT p_limit FOR UPDATE SKIP LOCKED $$;

GRANT EXECUTE ON FUNCTION app.outbox_pick(integer), app.outbox_purge(interval),
    app.inbox_purge(interval), app.idempotency_purge(interval),
    app.job_overdue(integer) TO qw_worker;
-- The relay writes only the publishing columns.
GRANT UPDATE (published_at, attempts, next_attempt_at, last_error, parked_at)
    ON app.outbox TO qw_worker;
GRANT INSERT (deadline_at) ON app.job TO qw_app, qw_worker;
GRANT SELECT (deadline_at) ON app.job TO qw_app;
