-- 0005: durable jobs, transactional outbox and consumer inbox (T025; spec 03
-- "Durable coordination"; T008 C-17, F-19). Delivery is at-least-once: jobs and
-- events can be delivered more than once and consumers deduplicate by event id.
-- All three tables are tenant-owned with FORCED row level security. Dispatch across
-- tenants (job_pick, job_expired_leases) goes through SECURITY DEFINER
-- functions with fixed SQL that return only ids and counters, never payloads; every
-- write happens under the job's own tenant context. Grants: qw_worker (claims,
-- transitions, outbox, inbox) and qw_app (enqueue, outbox insert, job status reads);
-- no PUBLIC, no DELETE or TRUNCATE.

CREATE TYPE app.job_state AS ENUM ('queued', 'running', 'succeeded', 'failed',
    'cancelled', 'expired', 'dead_letter');
-- Declaration order is claim order (enum comparison follows it).
CREATE TYPE app.job_priority AS ENUM ('safety', 'monitoring', 'interactive_research',
    'scheduled_research', 'experiment');
CREATE TYPE app.job_pool AS ENUM ('standard', 'experiment');

CREATE TABLE app.job (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    kind text NOT NULL CHECK (kind ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'),
    input_revision text NOT NULL CHECK (input_revision ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    -- Job-layer idempotency key (C-03): derived by the server, never client input.
    idempotency_key text GENERATED ALWAYS AS (kind || ':' || input_revision) STORED,
    priority app.job_priority NOT NULL,
    pool app.job_pool NOT NULL,
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 65536
        -- money travels as decimal strings: no fractional JSON numbers
        AND NOT jsonb_path_exists(payload, '$.** ? (@.type() == "number" && @ != @.floor())')),
    state app.job_state NOT NULL DEFAULT 'queued',
    -- attempts counts claims in the current generation; an operator requeue starts
    -- a new generation (at most 10). total_attempts never decreases.
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    generation integer NOT NULL DEFAULT 0 CHECK (generation BETWEEN 0 AND 10),
    total_attempts integer NOT NULL DEFAULT 0 CHECK (total_attempts >= attempts),
    -- Trigger time (R067): set at enqueue and frozen; next_available_at is the
    -- backoff wait and moves on retries.
    available_at timestamptz NOT NULL DEFAULT now(),
    next_available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text CHECK (char_length(lease_owner) BETWEEN 1 AND 200),
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    claimed_at timestamptz,
    last_error text CHECK (last_error ~ '^[a-z][a-z0-9_]{0,63}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kind, input_revision),
    UNIQUE (tenant_id, id),
    CHECK (attempts BETWEEN 0 AND max_attempts),
    CHECK ((priority = 'experiment') = (pool = 'experiment')),
    CHECK ((state = 'running') = (lease_owner IS NOT NULL)
        AND (lease_owner IS NULL) = (lease_expires_at IS NULL))
);
CREATE INDEX job_ready ON app.job (pool, priority, tenant_id, next_available_at)
    WHERE state = 'queued';
CREATE INDEX job_served ON app.job (pool, priority, tenant_id, claimed_at);
CREATE INDEX job_leases ON app.job (lease_expires_at) WHERE state = 'running';

-- Mirrors qw_domain.jobs.TRANSITIONS (a test compares them) and freezes identity.
CREATE FUNCTION app.job_update_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.kind, NEW.input_revision, NEW.priority, NEW.pool,
        NEW.payload, NEW.max_attempts, NEW.created_at, NEW.available_at)
        IS DISTINCT FROM
       (OLD.id, OLD.tenant_id, OLD.kind, OLD.input_revision, OLD.priority, OLD.pool,
        OLD.payload, OLD.max_attempts, OLD.created_at, OLD.available_at) THEN
        RAISE EXCEPTION 'app.job: identity, input, limits and trigger time are immutable';
    END IF;
    -- Attempt history: a new generation (attempts back to 0, total kept) only by
    -- the operator requeue dead_letter -> queued, exactly +1; otherwise attempts
    -- never fall and total_attempts grows by exactly the attempts increase.
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
CREATE TRIGGER job_update_guard BEFORE UPDATE ON app.job
    FOR EACH ROW EXECUTE FUNCTION app.job_update_guard();

CREATE TABLE app.outbox (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    event_type text NOT NULL
        CHECK (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 65536
        AND NOT jsonb_path_exists(payload, '$.** ? (@.type() == "number" && @ != @.floor())')),
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz CHECK (published_at >= created_at),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error text CHECK (last_error ~ '^[a-z][a-z0-9_]{0,63}$'),
    UNIQUE (tenant_id, id)
);
CREATE INDEX outbox_pending ON app.outbox (next_attempt_at) WHERE published_at IS NULL;

-- One row per (consumer, event) a consumer has processed, written in the same
-- transaction as the consumer's effect.
CREATE TABLE app.inbox (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    consumer text NOT NULL CHECK (consumer ~ '^[a-z][a-z0-9_.-]{0,63}$'),
    event_id uuid NOT NULL,
    processed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, consumer, event_id)
);

ALTER TABLE app.job ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.job FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.job USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.outbox
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.inbox
    USING (tenant_id = app.current_tenant_id());
-- Cross-tenant read and row lock for the owner, i.e. inside the definer functions
-- (FOR UPDATE needs an UPDATE policy). WITH CHECK (false): an owner UPDATE still
-- needs the row's tenant context (tenant_isolation).
CREATE POLICY dispatch ON app.job FOR SELECT TO qw_migrate USING (true);
CREATE POLICY dispatch_lock ON app.job FOR UPDATE TO qw_migrate
    USING (true) WITH CHECK (false);

-- Fair claim selection. Within the highest priority class that has ready work in
-- the pool, tenants are visited least-recently-served first (the latest claimed_at
-- of the tenant's jobs in that pool and class; never served sorts first), so with
-- equal weights claims rotate round-robin across tenants with ready work. The first
-- ready job of the first tenant whose row is not locked by another claimer is
-- locked (FOR UPDATE SKIP LOCKED, held by the caller's transaction) and returned.
CREATE FUNCTION app.job_pick(p_pool app.job_pool, OUT tenant_id uuid, OUT id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT q.tenant_id, q.priority FROM (
            SELECT DISTINCT j.tenant_id, j.priority FROM app.job j
            WHERE j.pool = p_pool AND j.state = 'queued'
              AND j.next_available_at <= now()) q
        ORDER BY q.priority, (SELECT max(s.claimed_at) FROM app.job s
            WHERE s.pool = p_pool AND s.priority = q.priority
              AND s.tenant_id = q.tenant_id) NULLS FIRST, q.tenant_id
    LOOP
        SELECT j.tenant_id, j.id INTO tenant_id, id FROM app.job j
            WHERE j.tenant_id = r.tenant_id AND j.pool = p_pool
              AND j.priority = r.priority AND j.state = 'queued'
              AND j.next_available_at <= now()
            ORDER BY j.next_available_at, j.created_at, j.id
            LIMIT 1 FOR UPDATE SKIP LOCKED;
        IF FOUND THEN
            RETURN;
        END IF;
    END LOOP;
END
$$;

-- Running jobs whose lease ran out, locked for the caller's transaction.
CREATE FUNCTION app.job_expired_leases(p_limit integer)
RETURNS TABLE (tenant_id uuid, id uuid, attempts integer, max_attempts integer)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT j.tenant_id, j.id, j.attempts, j.max_attempts FROM app.job j
      WHERE j.state = 'running' AND j.lease_expires_at <= now()
      ORDER BY j.lease_expires_at, j.id LIMIT p_limit FOR UPDATE SKIP LOCKED $$;

GRANT EXECUTE ON FUNCTION app.job_pick(app.job_pool), app.job_expired_leases(integer)
    TO qw_worker;
GRANT USAGE ON TYPE app.job_state, app.job_priority, app.job_pool TO qw_app, qw_worker;

GRANT INSERT (id, tenant_id, kind, input_revision, priority, pool, payload,
    max_attempts) ON app.job TO qw_app, qw_worker;
GRANT SELECT ON app.job TO qw_worker;
-- qw_app reads status but never lease_owner (the bearer lease token).
GRANT SELECT (id, tenant_id, kind, input_revision, idempotency_key, priority, pool,
    payload, state, attempts, max_attempts, generation, total_attempts, available_at,
    next_available_at, lease_expires_at, heartbeat_at, claimed_at, last_error,
    created_at, updated_at) ON app.job TO qw_app;
GRANT UPDATE (state, attempts, generation, total_attempts, next_available_at,
    lease_owner, lease_expires_at, heartbeat_at, claimed_at, last_error, updated_at)
    ON app.job TO qw_worker;
GRANT INSERT (id, tenant_id, event_type, payload) ON app.outbox TO qw_app, qw_worker;
GRANT SELECT ON app.outbox TO qw_worker;  -- relay UPDATE grants: increment 2
GRANT SELECT, INSERT (tenant_id, consumer, event_id) ON app.inbox TO qw_worker;
