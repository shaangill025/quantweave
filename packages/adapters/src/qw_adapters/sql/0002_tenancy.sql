-- 0002: tenancy (T010 increment 1; spec 03 "Storage and access", spec 12; T008 review
-- section 5, F-20). Every table here is tenant-owned. Row level security is enabled
-- and FORCED (the owner qw_migrate is bound too) and keys rows to the transaction
-- setting app.tenant_id, which qw_adapters.tenancy sets with set_config(..., true)
-- from a server-derived UUID. A missing or empty setting matches no row; a malformed
-- one fails the cast. A FOR ALL policy without WITH CHECK reuses USING for writes.
-- Child tables carry tenant_id inside composite foreign keys, so a row can never
-- reference another tenant's user or membership (referential checks ignore RLS).

CREATE TYPE app.tenant_status AS ENUM ('active', 'suspended');
-- Tenant-scoped human roles of T008 section 5. installation_operator, maintainer and
-- support are installation-scoped and are assigned outside tenant membership.
CREATE TYPE app.membership_role AS ENUM ('tenant_owner', 'tenant_member');

CREATE FUNCTION app.current_tenant_id() RETURNS uuid
LANGUAGE sql STABLE PARALLEL SAFE
AS $$ SELECT NULLIF(pg_catalog.current_setting('app.tenant_id', true), '')::uuid $$;

CREATE TABLE app.tenant (
    id uuid PRIMARY KEY,
    status app.tenant_status NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Application identity; provider identities (OIDC subject, password verifier) link to
-- it in a later increment and never replace it.
CREATE TABLE app.app_user (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE app.membership (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    user_id uuid NOT NULL,
    role app.membership_role NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.app_user (tenant_id, id)
);

-- Server-side sessions: only sha256(token) is stored. The runtime role cannot read
-- token_hash (column grants); lookup goes through the session_by_token policy.
CREATE TABLE app.session (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash bytea NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    step_up_at timestamptz,
    UNIQUE (tenant_id, id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.membership (tenant_id, user_id),
    CHECK (expires_at > created_at AND expires_at <= created_at + interval '30 days'),
    CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    CHECK (step_up_at IS NULL OR step_up_at >= created_at)
);

-- Only revoked_at (set once, to the transaction time) and step_up_at (to the
-- transaction time) may change; this binds the owner too. Whether a step-up really
-- happened is the API's responsibility (the runtime role can write the column).
CREATE FUNCTION app.session_update_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.id, NEW.tenant_id, NEW.user_id, NEW.token_hash, NEW.created_at,
        NEW.expires_at) IS DISTINCT FROM (OLD.id, OLD.tenant_id, OLD.user_id,
        OLD.token_hash, OLD.created_at, OLD.expires_at) THEN
        RAISE EXCEPTION 'app.session: only revoked_at and step_up_at may change';
    END IF;
    IF NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
        AND (OLD.revoked_at IS NOT NULL OR NEW.revoked_at <> now()) THEN
        RAISE EXCEPTION 'app.session: revoked_at is set once, to now()';
    END IF;
    IF NEW.step_up_at IS DISTINCT FROM OLD.step_up_at
        AND (NEW.step_up_at IS NULL OR NEW.step_up_at <> now()) THEN
        RAISE EXCEPTION 'app.session: step_up_at may only be set to now()';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER session_update_guard BEFORE UPDATE ON app.session
    FOR EACH ROW EXECUTE FUNCTION app.session_update_guard();

CREATE TABLE app.audit_event (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    actor_user_id uuid,
    action text NOT NULL CHECK (action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
    target_id uuid,
    correlation_id text CHECK (correlation_id ~ '^[A-Za-z0-9_-]{1,128}$'),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, actor_user_id) REFERENCES app.app_user (tenant_id, id)
);

-- Append-only, also for the owner and superusers: a statement trigger fires even
-- when RLS hides every row. Only the schema owner can disable it (deploy-time role).
CREATE FUNCTION app.audit_event_append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'app.audit_event is append-only: % refused', TG_OP;
END
$$;

CREATE TRIGGER audit_event_append_only
    BEFORE UPDATE OR DELETE OR TRUNCATE ON app.audit_event
    FOR EACH STATEMENT EXECUTE FUNCTION app.audit_event_append_only();

ALTER TABLE app.tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.tenant USING (id = app.current_tenant_id());

ALTER TABLE app.app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.app_user
    USING (tenant_id = app.current_tenant_id());

ALTER TABLE app.membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.membership FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.membership
    USING (tenant_id = app.current_tenant_id());

ALTER TABLE app.session ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.session FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.session
    USING (tenant_id = app.current_tenant_id());
-- Authentication happens before the tenant is known: with no tenant context, a
-- transaction that set app.session_token_hash (hex of sha256(token)) may read the
-- one row with that hash. Read-only; revocation needs the tenant context.
CREATE POLICY session_by_token ON app.session FOR SELECT
    USING (
        app.current_tenant_id() IS NULL
        AND token_hash = pg_catalog.decode(
            NULLIF(
                pg_catalog.current_setting('app.session_token_hash', true), ''
            ),
            'hex'
        )
    );

ALTER TABLE app.audit_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.audit_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.audit_event
    USING (tenant_id = app.current_tenant_id());

-- Least privilege: no DELETE or TRUNCATE anywhere; server-assigned columns
-- (created_at, occurred_at, version, status) are not insertable. Ids are always
-- generated by the server, never accepted from a client.
GRANT USAGE ON TYPE app.tenant_status, app.membership_role TO qw_app, qw_worker;
GRANT EXECUTE ON FUNCTION app.current_tenant_id() TO qw_app, qw_worker;

GRANT SELECT ON app.tenant, app.app_user, app.membership TO qw_app, qw_worker;
GRANT INSERT (id) ON app.tenant TO qw_app;
-- No UPDATE on tenant.status: suspension belongs to an operator path (not built).
GRANT INSERT (id, tenant_id, display_name) ON app.app_user TO qw_app;
GRANT UPDATE (display_name) ON app.app_user TO qw_app;
GRANT INSERT (tenant_id, user_id, role) ON app.membership TO qw_app;
GRANT UPDATE (role, version) ON app.membership TO qw_app;

GRANT SELECT (id, tenant_id, user_id, created_at, expires_at, revoked_at, step_up_at),
    INSERT (id, tenant_id, user_id, token_hash, expires_at),
    UPDATE (revoked_at, step_up_at)
    ON app.session TO qw_app;

GRANT SELECT, INSERT (id, tenant_id, actor_user_id, action, target_id, correlation_id)
    ON app.audit_event TO qw_app, qw_worker;
