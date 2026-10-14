-- 0017: notifications and delivery state (T056 increment 2; spec §13). Adapter:
-- qw_adapters.notification_store, which recomputes each record id (HMAC of the
-- dedupe key) and row seal (HMAC) under the server key and replays delivery events
-- through the domain transitions, failing closed on any difference.
-- notification: one per (tenant, user, category, object, version), UNIQUE; the
-- subject at trigger time (current state is resolved at read time, never stored).
-- notification_delivery(_event): external channels; state changes are new rows.
-- notification_seen: the owner's first open; delivery never writes it.
-- Append-only (owner too), forced RLS; created_at, event and seen times follow the
-- database clock. Producers and senders are qw_worker; qw_app reads and records an
-- open. Nothing here sends.

CREATE TABLE app.notification (
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    tenant_id uuid NOT NULL,
    id text NOT NULL CHECK (id ~ '^ntf-[0-9a-f]{32}$'),
    user_id uuid NOT NULL,
    category text NOT NULL CHECK (category IN ('proposal', 'monitoring_alert',
        'risk_pause', 'job_outcome')),
    object_id text NOT NULL CHECK (object_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    version text NOT NULL CHECK (length(version) BETWEEN 1 AND 160),
    trigger_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    listed boolean NOT NULL,
    seal text NOT NULL CHECK (seal ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, user_id, category, object_id, version),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.app_user (tenant_id, id),
    CHECK (created_at >= trigger_at)
);
CREATE INDEX notification_user_seq ON app.notification (tenant_id, user_id, seq);

CREATE TABLE app.notification_delivery (
    tenant_id uuid NOT NULL,
    notification_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('email', 'browser_push')),
    not_before timestamptz NOT NULL,
    seal text NOT NULL CHECK (seal ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, notification_id, channel),
    FOREIGN KEY (tenant_id, notification_id) REFERENCES app.notification (tenant_id, id)
);

CREATE TABLE app.notification_delivery_event (
    tenant_id uuid NOT NULL,
    notification_id text NOT NULL,
    channel text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    status text NOT NULL CHECK (status IN ('queued', 'attempted', 'delivered',
        'failed', 'unknown')),
    at timestamptz NOT NULL,
    code text NOT NULL CHECK (code ~ '^[a-z_]{0,64}$'),
    seal text NOT NULL CHECK (seal ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, notification_id, channel, position),
    FOREIGN KEY (tenant_id, notification_id, channel)
        REFERENCES app.notification_delivery (tenant_id, notification_id, channel)
);

CREATE TABLE app.notification_seen (
    tenant_id uuid NOT NULL,
    notification_id text NOT NULL,
    seen_at timestamptz NOT NULL,
    seal text NOT NULL CHECK (seal ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, notification_id),
    FOREIGN KEY (tenant_id, notification_id) REFERENCES app.notification (tenant_id, id)
);

CREATE FUNCTION app.notification_db_clock() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    t timestamptz := (to_jsonb(NEW) ->> TG_ARGV[0])::timestamptz;
BEGIN
    IF t > clock_timestamp() OR t < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % % is not the database clock', TG_TABLE_NAME,
            TG_ARGV[0], t USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER db_clock BEFORE INSERT ON app.notification
    FOR EACH ROW EXECUTE FUNCTION app.notification_db_clock('created_at');
CREATE TRIGGER db_clock BEFORE INSERT ON app.notification_delivery_event
    FOR EACH ROW EXECUTE FUNCTION app.notification_db_clock('at');
CREATE TRIGGER db_clock BEFORE INSERT ON app.notification_seen
    FOR EACH ROW EXECUTE FUNCTION app.notification_db_clock('seen_at');

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.notification FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.notification_delivery FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.notification_delivery_event FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.notification_seen FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.notification ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.notification USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.notification_delivery ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.notification_delivery USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.notification_delivery_event ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.notification_delivery_event USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.notification_seen ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.notification_seen USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.notification, app.notification_delivery,
    app.notification_delivery_event, app.notification_seen TO qw_app, qw_worker;
GRANT INSERT (tenant_id, id, user_id, category, object_id, version, trigger_at,
    created_at, listed, seal) ON app.notification TO qw_worker;
GRANT INSERT ON app.notification_delivery, app.notification_delivery_event TO qw_worker;
GRANT INSERT ON app.notification_seen TO qw_app;
