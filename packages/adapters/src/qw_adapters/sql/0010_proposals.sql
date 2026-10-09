-- 0010: proposal versions, lifecycle events and planning commitments (T026 increment
-- 2; spec §3, §7; T008 F-09, F-10). Mirrors qw_domain.proposals/commitments; the
-- adapter is qw_adapters.proposal_store. Capital is NOT checked here: the adapter
-- checks reservations against qualified cash under the account advisory lock.
-- - Versions and events are append-only (statement triggers, as in 0007); the event
--   trigger mirrors TRANSITIONS via app.proposal_step_ok (a test compares them):
--   gapless seq, (1, candidate) first, a new version only after supersede, the only
--   same-state event is active none -> planned, times never go back or ahead.
-- - A commitment is a planning reservation, never a broker one: held -> planned ->
--   released (or held -> released), never deleted; inserting or planning it needs
--   the proposal's latest event to be that version, active, same execution. At most
--   one live row per proposal. Version columns must agree with the content.
-- No SECURITY DEFINER function: every check runs as the writing role under RLS.

CREATE TYPE app.proposal_state AS ENUM ('candidate', 'verifying', 'awaiting_review',
    'ready_for_final_check', 'active', 'research_only', 'rejected', 'expired',
    'invalidated', 'dismissed', 'superseded');
CREATE TYPE app.proposal_execution AS ENUM ('none', 'planned', 'user_reported',
    'broker_confirmed', 'reconciled');
CREATE TYPE app.commitment_state AS ENUM ('held', 'planned', 'released');

CREATE TABLE app.proposal_version (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    proposal_id text NOT NULL CHECK (proposal_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    version integer NOT NULL CHECK (version >= 1),
    account_id text NOT NULL CHECK (account_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    sleeve_id text,
    group_id text CHECK (group_id <> ''),
    policy_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'
        AND octet_length(content::text) <= 65536),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, proposal_id, version)
);
CREATE INDEX proposal_version_scope ON app.proposal_version
    (tenant_id, account_id, currency, expires_at);
CREATE INDEX proposal_version_group ON app.proposal_version (tenant_id, group_id)
    WHERE group_id IS NOT NULL;

CREATE TABLE app.proposal_event (
    tenant_id uuid NOT NULL,
    proposal_id text NOT NULL,
    seq integer NOT NULL CHECK (seq >= 1),
    version integer NOT NULL,
    state app.proposal_state NOT NULL,
    execution app.proposal_execution NOT NULL,
    at timestamptz NOT NULL,  -- decision time: server clock, never earlier events
    code text CHECK (code ~ '^[A-Z][A-Z_]{0,63}$'),
    detail text NOT NULL CHECK (octet_length(detail) <= 1024),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, proposal_id, seq),
    FOREIGN KEY (tenant_id, proposal_id, version)
        REFERENCES app.proposal_version (tenant_id, proposal_id, version)
);

CREATE TABLE app.proposal_commitment (
    tenant_id uuid NOT NULL,
    proposal_id text NOT NULL,
    version integer NOT NULL,
    reserved_wire text NOT NULL
        CHECK (reserved_wire ~ '^(0|[1-9][0-9]{0,25})(\.[0-9]{0,11}[1-9])?$'),
    reserved numeric(38, 12) GENERATED ALWAYS AS (reserved_wire::numeric(38, 12)) STORED,
    mark_wire text CHECK (mark_wire ~ '^(0|[1-9][0-9]{0,25})(\.[0-9]{0,11}[1-9])?$'),
    state app.commitment_state NOT NULL DEFAULT 'held',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, proposal_id, version),
    FOREIGN KEY (tenant_id, proposal_id, version)
        REFERENCES app.proposal_version (tenant_id, proposal_id, version)
);
CREATE UNIQUE INDEX proposal_commitment_live ON app.proposal_commitment
    (tenant_id, proposal_id) WHERE state <> 'released';

CREATE FUNCTION app.proposal_step_ok(src app.proposal_state, dst app.proposal_state)
RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path = pg_catalog
AS $$
    SELECT CASE src::text
        WHEN 'candidate' THEN dst::text IN ('verifying', 'research_only', 'rejected')
        WHEN 'verifying' THEN dst::text IN ('awaiting_review', 'ready_for_final_check',
            'research_only', 'rejected')
        WHEN 'awaiting_review' THEN dst::text IN ('ready_for_final_check',
            'research_only', 'rejected')
        WHEN 'ready_for_final_check' THEN dst::text IN ('active', 'rejected')
        WHEN 'active' THEN dst::text = 'dismissed'
        ELSE false END
    OR (src::text IN ('candidate', 'verifying', 'awaiting_review',
            'ready_for_final_check', 'active')
        AND dst::text IN ('expired', 'invalidated', 'superseded'))
$$;

CREATE FUNCTION app.proposal_insert_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    prev record;
    a jsonb;
    ok boolean;
BEGIN
    IF TG_TABLE_NAME = 'proposal_version' THEN
        a := NEW.content -> 'action';
        SELECT max(version) INTO prev FROM app.proposal_version
            WHERE tenant_id = NEW.tenant_id AND proposal_id = NEW.proposal_id;
        IF NEW.version <> coalesce(prev.max, 0) + 1
            OR (NEW.content ->> 'proposal_id', (NEW.content ->> 'version')::integer,
                a ->> 'tenant_id', a ->> 'account_id', a ->> 'currency',
                a ->> 'sleeve_id', NEW.content ->> 'alternatives_group_id',
                (NEW.content ->> 'expires_at')::timestamptz)
            IS DISTINCT FROM (NEW.proposal_id, NEW.version, NEW.tenant_id::text,
                NEW.account_id, NEW.currency, NEW.sleeve_id, NEW.group_id,
                NEW.expires_at) THEN
            RAISE EXCEPTION 'app.proposal_version: next version, columns match content';
        END IF;
        RETURN NEW;
    END IF;
    SELECT seq, version, state, execution, at INTO prev FROM app.proposal_event
        WHERE tenant_id = NEW.tenant_id AND proposal_id = NEW.proposal_id
        ORDER BY seq DESC LIMIT 1;
    IF NEW.at > clock_timestamp() OR (FOUND AND NEW.at < prev.at) THEN
        RAISE EXCEPTION 'app.proposal_event: decision times never go back or ahead';
    END IF;
    IF NOT FOUND THEN
        ok := (NEW.seq, NEW.version, NEW.state::text, NEW.execution::text)
            = (1, 1, 'candidate', 'none');
    ELSIF NEW.seq <> prev.seq + 1 THEN
        ok := false;
    ELSIF NEW.version = prev.version + 1 THEN
        ok := prev.state = 'superseded' AND NEW.state = 'candidate'
            AND NEW.execution = 'none';
    ELSIF NEW.version <> prev.version THEN
        ok := false;
    ELSIF NEW.state = prev.state THEN
        ok := NEW.state = 'active' AND prev.execution = 'none'
            AND NEW.execution = 'planned';
    ELSE
        ok := app.proposal_step_ok(prev.state, NEW.state)
            AND NEW.execution = prev.execution;
    END IF;
    IF NOT ok THEN
        RAISE EXCEPTION 'app.proposal_event: illegal transition % -> %',
            coalesce(prev.state::text, '-'), NEW.state;
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION app.proposal_commitment_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    last record;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.tenant_id, NEW.proposal_id, NEW.version, NEW.reserved_wire,
            NEW.mark_wire, NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,
            OLD.proposal_id, OLD.version, OLD.reserved_wire, OLD.mark_wire,
            OLD.created_at) OR (OLD.state::text, NEW.state::text) NOT IN (
            ('held', 'planned'), ('held', 'released'), ('planned', 'released')) THEN
            RAISE EXCEPTION 'app.proposal_commitment: illegal change % -> %',
                OLD.state, NEW.state;
        END IF;
        NEW.updated_at := now();
        IF NEW.state = 'released' THEN
            RETURN NEW;
        END IF;
    ELSIF NEW.state <> 'held' THEN
        RAISE EXCEPTION 'app.proposal_commitment: a commitment starts held';
    END IF;
    SELECT version, state, execution INTO last FROM app.proposal_event
        WHERE tenant_id = NEW.tenant_id AND proposal_id = NEW.proposal_id
        ORDER BY seq DESC LIMIT 1;
    IF NOT FOUND OR last.version <> NEW.version OR last.state <> 'active'
        OR (NEW.state = 'planned') <> (last.execution = 'planned') THEN
        RAISE EXCEPTION 'app.proposal_commitment: only the active latest version';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.proposal_version
    FOR EACH ROW EXECUTE FUNCTION app.proposal_insert_guard();
CREATE TRIGGER insert_guard BEFORE INSERT ON app.proposal_event
    FOR EACH ROW EXECUTE FUNCTION app.proposal_insert_guard();
CREATE TRIGGER guard BEFORE INSERT OR UPDATE ON app.proposal_commitment
    FOR EACH ROW EXECUTE FUNCTION app.proposal_commitment_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.proposal_version
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.proposal_event
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE DELETE OR TRUNCATE ON app.proposal_commitment
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.proposal_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.proposal_version FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.proposal_version
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.proposal_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.proposal_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.proposal_event
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.proposal_commitment ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.proposal_commitment FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.proposal_commitment
    USING (tenant_id = app.current_tenant_id());

-- Decisions run as qw_app; the expiry sweep may run as qw_worker.
GRANT USAGE ON TYPE app.proposal_state, app.proposal_execution, app.commitment_state
    TO qw_app, qw_worker;
GRANT EXECUTE ON FUNCTION app.proposal_step_ok(app.proposal_state,
    app.proposal_state) TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, proposal_id, version, account_id, currency, sleeve_id,
    group_id, policy_id, expires_at, content_hash, content) ON app.proposal_version
    TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, proposal_id, seq, version, state, execution, at, code,
    detail) ON app.proposal_event TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, proposal_id, version, reserved_wire, mark_wire),
    UPDATE (state) ON app.proposal_commitment TO qw_app, qw_worker;
