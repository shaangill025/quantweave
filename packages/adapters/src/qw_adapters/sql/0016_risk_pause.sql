-- 0016: risk pauses and explicit resumes (T020 increment 2; spec §5, §7 "Scoped
-- loss pauses"; T008 C-04, C-20). Mirrors qw_domain.risk_pauses; the adapter
-- (qw_adapters.pause_store) re-hashes every event and replays it through PauseBook.
-- - Append-only under forced tenant RLS: statement triggers refuse UPDATE, DELETE
--   and TRUNCATE for every role (owners and superusers can bypass triggers, as in
--   0007). A pause ends only by a resume row, at most one per pause.
-- - qw_app: SELECT and column INSERT. A pause names a member. A resume names a
--   tenant_owner's own live session, its current step_up_at (at most 1 hour old)
--   and the bound receipt id (the 0009 rule); a material, stale-data or
--   reconciliation pause also needs a reconciled revision id (form only, T017).
--   qw_worker: nothing yet.
-- - Event times are the DB clock (at most 60 s back, never ahead; 0013/0015), a
--   resume never precedes its pause, a pause never the tenant's previous one.
--   Inserts serialise on the advisory lock 'risk_pause:<tenant>'.

CREATE TABLE app.risk_pause (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    pause_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    policy_version integer NOT NULL,
    scope_kind text NOT NULL
        CHECK (scope_kind IN ('tenant', 'account', 'sleeve', 'strategy')),
    scope_id text NOT NULL CHECK (scope_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    trigger_kind text NOT NULL CHECK (trigger_kind IN ('drawdown_breach',
        'planned_loss_breach', 'loss_limit_breach', 'stale_data',
        'reconciliation_flag', 'user_pause')),
    evidence_ref text NOT NULL
        CHECK (evidence_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    material boolean NOT NULL,
    principal_id uuid NOT NULL,
    reason text NOT NULL CHECK (octet_length(reason) <= 4096),
    acknowledgement_text text NOT NULL
        CHECK (octet_length(acknowledgement_text) <= 4096),
    triggered_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, pause_id),
    CHECK (scope_kind <> 'tenant' OR scope_id = tenant_id::text),
    FOREIGN KEY (tenant_id, policy_id, policy_version)
        REFERENCES app.policy_version (tenant_id, policy_id, version),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES app.membership (tenant_id, user_id)
);

CREATE TABLE app.risk_pause_resume (
    tenant_id uuid NOT NULL,
    pause_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    session_id uuid NOT NULL,
    step_up_at timestamptz NOT NULL,
    step_up_receipt_id text NOT NULL,
    reason text NOT NULL CHECK (length(btrim(reason)) >= 1
        AND octet_length(reason) <= 4096),
    acknowledgement_text text NOT NULL
        CHECK (octet_length(acknowledgement_text) <= 4096),
    reconciled_revision text
        CHECK (reconciled_revision ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    resumed_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, pause_id),
    FOREIGN KEY (tenant_id, pause_id) REFERENCES app.risk_pause (tenant_id, pause_id),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES app.membership (tenant_id, user_id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES app.session (tenant_id, id)
);

CREATE FUNCTION app.risk_pause_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    at timestamptz;
    last timestamptz;
    p record;
    s record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'risk_pause:' || NEW.tenant_id::text, 0));
    IF TG_TABLE_NAME = 'risk_pause' THEN
        at := NEW.triggered_at;
    ELSE
        at := NEW.resumed_at;
    END IF;
    IF at > clock_timestamp() OR at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock', TG_TABLE_NAME, at
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'risk_pause' THEN
        SELECT max(triggered_at) INTO last FROM app.risk_pause
            WHERE tenant_id = NEW.tenant_id;
        IF at < last THEN
            RAISE EXCEPTION 'app.risk_pause: triggered_at is before the previous pause'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;
    SELECT triggered_at, material, trigger_kind INTO p FROM app.risk_pause
        WHERE tenant_id = NEW.tenant_id AND pause_id = NEW.pause_id;
    IF FOUND AND at < p.triggered_at THEN
        RAISE EXCEPTION 'app.risk_pause_resume: resumed before the pause'
            USING ERRCODE = 'check_violation';
    END IF;
    IF FOUND AND NEW.reconciled_revision IS NULL AND (p.material
        OR p.trigger_kind IN ('stale_data', 'reconciliation_flag')) THEN
        RAISE EXCEPTION 'app.risk_pause_resume: a reconciled revision is required';
    END IF;
    SELECT x.user_id, x.step_up_at, x.revoked_at, x.expires_at, m.role INTO s
        FROM app.session x JOIN app.membership m USING (tenant_id, user_id)
        WHERE x.tenant_id = NEW.tenant_id AND x.id = NEW.session_id;
    IF NOT FOUND OR s.user_id <> NEW.principal_id OR s.role <> 'tenant_owner'
        OR s.revoked_at IS NOT NULL OR s.expires_at <= now()
        OR s.step_up_at IS DISTINCT FROM NEW.step_up_at
        OR NEW.step_up_at < now() - interval '1 hour'
        OR NEW.step_up_receipt_id <> NEW.session_id::text || '.'
            || (extract(epoch FROM NEW.step_up_at) * 1000000)::bigint::text THEN
        RAISE EXCEPTION 'app.risk_pause_resume: no valid owner step-up';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.risk_pause
    FOR EACH ROW EXECUTE FUNCTION app.risk_pause_guard();
CREATE TRIGGER insert_guard BEFORE INSERT ON app.risk_pause_resume
    FOR EACH ROW EXECUTE FUNCTION app.risk_pause_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.risk_pause
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.risk_pause_resume
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.risk_pause ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.risk_pause
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.risk_pause_resume ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.risk_pause_resume
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT, INSERT (tenant_id, pause_id, policy_id, policy_version, scope_kind,
    scope_id, trigger_kind, evidence_ref, material, principal_id, reason,
    acknowledgement_text, triggered_at, event_hash) ON app.risk_pause TO qw_app;
GRANT SELECT, INSERT (tenant_id, pause_id, principal_id, session_id, step_up_at,
    step_up_receipt_id, reason, acknowledgement_text, reconciled_revision, resumed_at,
    event_hash) ON app.risk_pause_resume TO qw_app;
