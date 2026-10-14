-- 0013: data and rights registry (T021 increment 2; spec §6 capability registry,
-- §16 provider rights and status vocabulary, §3 global versus tenant data,
-- ADR-004). Mirrors qw_domain.rights; the adapter is qw_adapters.rights_store, which
-- reloads every history through the domain Registry (replay re-derives hashes,
-- numbering, transition and evidence rules), so a forged row fails closed.
-- - Global (installation) facts: rights_provider, rights_feed, rights_feed_revision,
--   rights_feed_transition. Runtime roles (qw_app, qw_worker) may only read them.
--   Only the schema owner qw_migrate (operator-held; future admin tooling) may
--   insert, through an INSERT policy limited to that role. RLS stays forced so every
--   app table keeps one catalogue invariant; the read policy is deliberately open.
-- - Tenant facts: rights_entitlement, under the forced tenant isolation policy.
--   Runtime roles read only; the owner inserts inside a tenant transaction.
-- - Append-only: revocation (an inactive or denied revision, a suspended or retired
--   transition) and expiry (a grant's expires_at) are recorded rows, never updates.
--   Statement triggers refuse UPDATE, DELETE and TRUNCATE, for the owner too.
-- - Knowledge times (revision recorded_at, transition at, entitlement recorded_at)
--   follow the database clock: never in the future, at most 60 seconds of
--   back-dating, and never before the previous row of the same feed (revisions and
--   transitions) or entitlement. The window is an owner design gate. A raw owner
--   insert out of time order that the domain would refuse makes the whole
--   registry unloadable (fail closed); repair needs an owner disabling triggers.
-- Nothing is seeded: the registry ships empty, and every use is denied.

CREATE TABLE app.rights_provider (
    id text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.rights_feed (
    id text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    provider_id text NOT NULL REFERENCES app.rights_provider (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.rights_feed_revision (
    feed_id text NOT NULL REFERENCES app.rights_feed (id),
    revision integer NOT NULL CHECK (revision >= 1),
    feed text NOT NULL CHECK (feed ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    required_uses jsonb NOT NULL CHECK (jsonb_typeof(required_uses) = 'array'),
    rights jsonb NOT NULL CHECK (jsonb_typeof(rights) = 'object'
        AND octet_length(rights::text) <= 65536),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (feed_id, revision)
);

CREATE TABLE app.rights_feed_transition (
    feed_id text NOT NULL REFERENCES app.rights_feed (id),
    position integer NOT NULL CHECK (position >= 0),
    status text NOT NULL CHECK (status IN ('registered', 'connected', 'qualified',
                                           'suspended', 'retired')),
    actor_id text NOT NULL CHECK (actor_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence) = 'array'
        AND octet_length(evidence::text) <= 65536),
    at timestamptz NOT NULL,
    revision integer NOT NULL,
    revision_hash text NOT NULL CHECK (revision_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (feed_id, position),
    FOREIGN KEY (feed_id, revision) REFERENCES app.rights_feed_revision (feed_id, revision)
);

CREATE TABLE app.rights_entitlement (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    feed_id text NOT NULL REFERENCES app.rights_feed (id),
    revision integer NOT NULL CHECK (revision >= 1),
    source text NOT NULL CHECK (source IN ('user_credential', 'installation_license')),
    active boolean NOT NULL,
    rights jsonb NOT NULL CHECK (jsonb_typeof(rights) = 'object'
        AND octet_length(rights::text) <= 65536),
    pooling_consent_ref text
        CHECK (pooling_consent_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, feed_id, revision)
);

CREATE FUNCTION app.rights_known_now() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    known timestamptz := (to_jsonb(NEW) ->> TG_ARGV[0])::timestamptz;
    fid text := to_jsonb(NEW) ->> 'feed_id';
    last timestamptz;
BEGIN
    IF known > clock_timestamp() OR known < clock_timestamp() - interval '60 seconds'
    THEN
        RAISE EXCEPTION 'app.%: % % is not the database clock', TG_TABLE_NAME,
            TG_ARGV[0], known USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'rights_entitlement' THEN
        SELECT max(e.recorded_at) INTO last FROM app.rights_entitlement e
            WHERE e.tenant_id = NEW.tenant_id AND e.feed_id = fid;
    ELSE
        SELECT greatest((SELECT max(r.recorded_at) FROM app.rights_feed_revision r
                         WHERE r.feed_id = fid),
                        (SELECT max(t.at) FROM app.rights_feed_transition t
                         WHERE t.feed_id = fid)) INTO last;
    END IF;
    IF known < last THEN
        RAISE EXCEPTION 'app.%: % % is before the previous row (%)', TG_TABLE_NAME,
            TG_ARGV[0], known, last USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER known_now BEFORE INSERT ON app.rights_feed_revision
    FOR EACH ROW EXECUTE FUNCTION app.rights_known_now('recorded_at');
CREATE TRIGGER known_now BEFORE INSERT ON app.rights_feed_transition
    FOR EACH ROW EXECUTE FUNCTION app.rights_known_now('at');
CREATE TRIGGER known_now BEFORE INSERT ON app.rights_entitlement
    FOR EACH ROW EXECUTE FUNCTION app.rights_known_now('recorded_at');

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.rights_provider FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.rights_feed FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.rights_feed_revision FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.rights_feed_transition FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.rights_entitlement FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.rights_provider ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY global_read ON app.rights_provider FOR SELECT USING (true);
CREATE POLICY owner_insert ON app.rights_provider FOR INSERT TO qw_migrate WITH CHECK (true);
ALTER TABLE app.rights_feed ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY global_read ON app.rights_feed FOR SELECT USING (true);
CREATE POLICY owner_insert ON app.rights_feed FOR INSERT TO qw_migrate WITH CHECK (true);
ALTER TABLE app.rights_feed_revision ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY global_read ON app.rights_feed_revision FOR SELECT USING (true);
CREATE POLICY owner_insert ON app.rights_feed_revision FOR INSERT TO qw_migrate WITH CHECK (true);
ALTER TABLE app.rights_feed_transition ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY global_read ON app.rights_feed_transition FOR SELECT USING (true);
CREATE POLICY owner_insert ON app.rights_feed_transition FOR INSERT TO qw_migrate WITH CHECK (true);
ALTER TABLE app.rights_entitlement ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.rights_entitlement USING (tenant_id = app.current_tenant_id());

-- Read-only for the runtime roles; no runtime role can grant or revoke a right.
GRANT SELECT ON app.rights_provider, app.rights_feed, app.rights_feed_revision,
    app.rights_feed_transition, app.rights_entitlement TO qw_app, qw_worker;
