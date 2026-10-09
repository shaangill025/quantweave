-- 0001: database roles of the protected control plane (T008 review section 5) and a
-- least-privilege baseline. This bootstrap migration runs as the session user, which
-- must be a superuser or a CREATEROLE role that is a member of qw_migrate. Later
-- migrations run as qw_migrate. Idempotent: roles are cluster-wide and may already
-- exist from another database; their attributes are reset to the safe set.
-- Login roles are created at deploy time as members of these group roles.

DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY[
        'qw_migrate', 'qw_app', 'qw_worker', 'qw_ingest', 'qw_assessor', 'qw_release'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
        EXECUTE format(
            'ALTER ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
            'NOREPLICATION NOBYPASSRLS INHERIT', r);
    END LOOP;
END
$$;

-- Only the listed roles may connect; PUBLIC loses CONNECT and TEMPORARY.
DO $$
BEGIN
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO qw_migrate, qw_app, qw_worker, qw_ingest, '
        'qw_assessor, qw_release', current_database());
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Application schema, owned by qw_migrate. Runtime roles get USAGE only; table
-- privileges are granted per table by the migration that creates it.
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION qw_migrate;
ALTER SCHEMA app OWNER TO qw_migrate;
REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO qw_app, qw_worker, qw_ingest, qw_assessor, qw_release;

-- Functions created by qw_migrate are not executable by PUBLIC unless granted.
ALTER DEFAULT PRIVILEGES FOR ROLE qw_migrate REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
