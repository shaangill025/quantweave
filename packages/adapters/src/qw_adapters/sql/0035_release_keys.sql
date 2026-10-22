-- 0035: the operator setup that backs the release reads (owner decision 45,
-- 2026-10-22, with decision 5 of 2026-10-20: a read goes live only once real
-- storage backs it). The operator command `qw-release-admin` runs as qw_release
-- (an exactly-qw_release login): it records this installation's deployment (0029
-- `release_deployment`) and its trusted maintainer keys, and revokes a key.
-- Installation scope, like 0029: no tenant id, forced RLS with per-role policies,
-- qw_app has no privilege. qw_worker reads the keys (it verifies releases at
-- record, install and rollback) and can never write one: generated experiment code
-- runs under the worker and must not control keys.
-- PUBLIC key material only. A non-test key stores its public key; the store admits
-- only algorithms in `qw_domain.release.PRODUCTION_ALGORITHMS`, empty until T009
-- reviews an asymmetric scheme, so today no non-test key can be added. A test-only
-- key (the symmetric test HMAC, whose key is a secret) stores no material at all:
-- `public_key` is NULL exactly when `test_only`; its verifier exists only in the
-- process that supplies it (the store's test hook). Nothing here can hold a secret
-- by design, but the database cannot tell a secret from public bytes: the store
-- also refuses PEM private-key material, and the operator is the control.
-- Revocation is append-only, at most one per key, at the database clock (never
-- back-dated, so an install it signed earlier still replays; reads after it see
-- `key_not_valid`). Rows are hashed by the store and re-verified on every read.

CREATE TABLE app.release_trusted_key (
    key_id text PRIMARY KEY CHECK (key_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    principal_id text NOT NULL
        CHECK (principal_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    algorithm text NOT NULL CHECK (algorithm ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    public_key text CHECK (char_length(public_key) BETWEEN 1 AND 8192),
    test_only boolean NOT NULL,
    valid_from timestamptz NOT NULL,
    valid_until timestamptz,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    CHECK (test_only = (public_key IS NULL)),
    CHECK (valid_until IS NULL OR valid_until > valid_from)
);

CREATE TABLE app.release_key_revocation (
    key_id text PRIMARY KEY REFERENCES app.release_trusted_key (key_id),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500
        AND reason !~ '[[:cntrl:]]'),
    revoked_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$')
);

CREATE FUNCTION app.release_key_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    ts timestamptz;
BEGIN
    IF TG_TABLE_NAME = 'release_key_revocation' THEN
        ts := NEW.revoked_at;
    ELSE
        ts := NEW.recorded_at;
    END IF;
    IF ts > clock_timestamp() OR ts < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock', TG_TABLE_NAME, ts
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['release_trusted_key', 'release_key_revocation'] LOOP
        EXECUTE format('CREATE TRIGGER insert_guard BEFORE INSERT ON app.%I '
            'FOR EACH ROW EXECUTE FUNCTION app.release_key_guard()', t);
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR '
            'TRUNCATE ON app.%I FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.journal_append_only()', t);
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY, '
            'FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY installation_operator ON app.%I TO qw_release '
            'USING (true) WITH CHECK (true)', t);
        EXECUTE format('CREATE POLICY installation_reader ON app.%I FOR SELECT '
            'TO qw_worker USING (true)', t);
        EXECUTE format('GRANT SELECT ON app.%I TO qw_release, qw_worker', t);
    END LOOP;
END
$$;

GRANT INSERT (key_id, principal_id, algorithm, public_key, test_only, valid_from,
    valid_until, recorded_at, event_hash) ON app.release_trusted_key TO qw_release;
GRANT INSERT (key_id, reason, revoked_at, event_hash)
    ON app.release_key_revocation TO qw_release;

-- The operator records the deployment. Owner decision 47 (2026-10-22, "Operator
-- only"): qw_worker loses 0029's write on it (untrusted generated code runs as the
-- worker and must not rewrite who can see release history) and keeps the read its
-- ledger replay needs.
REVOKE INSERT ON app.release_deployment FROM qw_worker;
DROP POLICY installation_worker ON app.release_deployment;
CREATE POLICY installation_worker_read ON app.release_deployment FOR SELECT
    TO qw_worker USING (true);
CREATE POLICY installation_operator ON app.release_deployment TO qw_release
    USING (true) WITH CHECK (true);
GRANT SELECT ON app.release_deployment TO qw_release;
GRANT INSERT (deployment_id, edition, operators, tenants, initial_schema,
    allows_test_keys, recorded_at, event_hash, solo_maintainer)
    ON app.release_deployment TO qw_release;
