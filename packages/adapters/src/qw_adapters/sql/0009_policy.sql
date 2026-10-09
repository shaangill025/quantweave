-- 0009: policy versions and adoption receipts (T019 increment 2; spec §7 "Policy as a
-- versioned contract"; T008 C-04, C-07, C-20). Mirrors qw_domain.policy; the adapter
-- is qw_adapters.policy_store.
-- - Both tables are append-only: runtime roles get SELECT and column INSERT only, and
--   statement triggers refuse any role's UPDATE, DELETE and TRUNCATE (the owner and a
--   superuser can still bypass triggers, as in 0007). Status is derived, never stored.
-- - Inserts for one policy serialise on a transaction advisory lock (the key matches
--   policy_store._LOCK). A version must be the latest + 1. An adoption names the
--   latest version, its content hash and label (composite FK); the primary key lets a
--   version be adopted at most once.
-- - created_at and signed_at are server-assigned (default now(), not insertable).
-- - Step-up binding (C-20): an adoption names the adopter's own live session and that
--   session's current step_up_at, at most 1 hour old (the Settings maximum; the API
--   applies its configured window). step_up_receipt_id is
--   '<session id>.<step_up_at in epoch microseconds>'.
-- No SECURITY DEFINER function: every check runs as the inserting role under RLS.

CREATE TABLE app.policy_version (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    policy_id uuid NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'
        AND octet_length(content::text) <= 262144),
    label text NOT NULL CHECK (label IN ('SYNTHETIC', 'LIVE')),
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by uuid NOT NULL,
    PRIMARY KEY (tenant_id, policy_id, version),
    UNIQUE (tenant_id, policy_id, version, content_hash, label),
    FOREIGN KEY (tenant_id, created_by) REFERENCES app.app_user (tenant_id, id)
);

CREATE TABLE app.policy_adoption (
    tenant_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    version integer NOT NULL,
    policy_hash text NOT NULL,
    label text NOT NULL,
    principal_id uuid NOT NULL,
    session_id uuid NOT NULL,
    step_up_at timestamptz NOT NULL,
    step_up_receipt_id text NOT NULL,
    signed_at timestamptz NOT NULL DEFAULT now(),
    limits jsonb NOT NULL CHECK (jsonb_typeof(limits) = 'array'
        AND jsonb_array_length(limits) >= 1),
    acknowledged jsonb NOT NULL CHECK (jsonb_typeof(acknowledged) = 'array'),
    exclusions jsonb NOT NULL CHECK (jsonb_typeof(exclusions) = 'array'),
    acknowledgement_text text NOT NULL
        CHECK (octet_length(acknowledgement_text) <= 4096),
    receipt_hash text NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, policy_id, version),
    FOREIGN KEY (tenant_id, policy_id, version, policy_hash, label)
        REFERENCES app.policy_version (tenant_id, policy_id, version, content_hash,
            label),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES app.membership (tenant_id, user_id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES app.session (tenant_id, id)
);

CREATE FUNCTION app.policy_insert_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    latest integer;
    s record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'policy/' || NEW.tenant_id::text || '/' || NEW.policy_id::text, 0));
    SELECT max(version) INTO latest FROM app.policy_version
        WHERE tenant_id = NEW.tenant_id AND policy_id = NEW.policy_id;
    IF TG_TABLE_NAME = 'policy_version' THEN
        IF NEW.version <> coalesce(latest, 0) + 1 THEN
            RAISE EXCEPTION 'app.policy_version: version must be the latest + 1';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.version IS DISTINCT FROM latest THEN
        RAISE EXCEPTION 'app.policy_adoption: only the latest version is adoptable';
    END IF;
    SELECT user_id, step_up_at, revoked_at, expires_at INTO s FROM app.session
        WHERE tenant_id = NEW.tenant_id AND id = NEW.session_id;
    IF NOT FOUND OR s.user_id <> NEW.principal_id OR s.revoked_at IS NOT NULL
        OR s.expires_at <= now() OR s.step_up_at IS DISTINCT FROM NEW.step_up_at
        OR NEW.step_up_at < now() - interval '1 hour'
        OR NEW.step_up_receipt_id <> NEW.session_id::text || '.'
            || (extract(epoch FROM NEW.step_up_at) * 1000000)::bigint::text THEN
        RAISE EXCEPTION 'app.policy_adoption: no valid step-up by the principal';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.policy_version
    FOR EACH ROW EXECUTE FUNCTION app.policy_insert_guard();
CREATE TRIGGER insert_guard BEFORE INSERT ON app.policy_adoption
    FOR EACH ROW EXECUTE FUNCTION app.policy_insert_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.policy_version
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.policy_adoption
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.policy_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.policy_version FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.policy_version
    USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.policy_adoption ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.policy_adoption FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.policy_adoption
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT, INSERT (tenant_id, policy_id, version, content_hash, content, label,
    created_by) ON app.policy_version TO qw_app;
GRANT SELECT, INSERT (tenant_id, policy_id, version, policy_hash, label, principal_id,
    session_id, step_up_at, step_up_receipt_id, limits, acknowledged, exclusions,
    acknowledgement_text, receipt_hash) ON app.policy_adoption TO qw_app;
