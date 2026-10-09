-- 0003: local credentials (T010 increment 2; spec 12 "password verifiers"). Tenant-
-- owned, with ENABLEd and FORCEd row level security keyed to app.current_tenant_id(),
-- a composite foreign key to app_user, and grants to qw_app only (qw_worker gets
-- nothing; never PUBLIC; no DELETE or TRUNCATE).

-- Local (self-hosted) credentials: one installation-unique login per application
-- user. Only an argon2id PHC string is stored; hashing happens in apps/api.
CREATE TABLE app.local_credential (
    tenant_id uuid NOT NULL,
    user_id uuid NOT NULL,
    login text NOT NULL UNIQUE CHECK (login ~ '^[a-z0-9][a-z0-9._@-]{0,127}$'),
    verifier text NOT NULL CHECK (verifier ~ '^\$argon2id\$v=19\$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES app.app_user (tenant_id, id)
);

ALTER TABLE app.local_credential ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.local_credential FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.local_credential
    USING (tenant_id = app.current_tenant_id());
-- Login happens before the tenant is known: with no tenant context, a transaction
-- that set app.login may read the one row with that login. Read-only.
CREATE POLICY credential_by_login ON app.local_credential FOR SELECT
    USING (
        app.current_tenant_id() IS NULL
        AND login = NULLIF(pg_catalog.current_setting('app.login', true), '')
    );

-- No UPDATE: credential rotation and rehash are later work (T010 receipt).
GRANT SELECT, INSERT (tenant_id, user_id, login, verifier)
    ON app.local_credential TO qw_app;
