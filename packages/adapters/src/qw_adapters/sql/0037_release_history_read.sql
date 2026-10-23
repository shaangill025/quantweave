-- Release and install history read (project contract 0.12.0, listReleases; owner
-- decisions 5 of 2026-10-20, 45, 47 and 48 of 2026-10-22, and 55, 56 and 58 of
-- 2026-10-23).
-- qw_app keeps no privilege on the five release tables (0029, 0035). It reads
-- through the SECURITY DEFINER functions below (owner qw_migrate, search_path
-- pg_catalog, EXECUTE qw_app only), each of which returns rows only when the
-- session tenant (app.current_tenant_id(), set by the tenant transaction) is a
-- member of the recorded deployment; an unknown deployment and a non-member read
-- alike (no rows). They return only what the read needs:
-- - the deployment without its tenant list (cross-tenant data): `tenants` is the
--   caller's own id, and `hash_ok` is the stored event_hash re-computed here over
--   the full row (the store's canonical JSON, sha256 built in, no extension), so
--   the list never leaves the database;
-- - its events in seq order, the signed releases they name, and the trusted keys
--   and revocations (public material only; test keys store none, 0035).
-- The store (qw_adapters.release_store.history) re-hashes every other row,
-- rebuilds the manifests and replays the ledger with full signature checks
-- against these stored keys. Forced RLS binds the owner too, so qw_migrate gets
-- SELECT-only policies on the five tables (the 0005/0006 pattern); qw_migrate
-- already owns them.

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['release_deployment', 'signed_release',
            'deployment_event', 'release_trusted_key', 'release_key_revocation'] LOOP
        EXECUTE format('CREATE POLICY history_reader ON app.%I FOR SELECT '
            'TO qw_migrate USING (true)', t);
    END LOOP;
END
$$;

-- Not SECURITY DEFINER and not granted: it runs inside the functions below, as
-- their owner.
CREATE FUNCTION app.release_history_member(p_deployment text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog
AS $$ SELECT EXISTS (SELECT 1 FROM app.release_deployment d
    WHERE d.deployment_id = p_deployment
      AND app.current_tenant_id()::text = ANY (d.tenants)) $$;

-- `qw_domain.instants.format_instant` and `research_data.digest` (sorted keys,
-- "," and ":" separators, raw non-ASCII) of the store's deployment wire.
CREATE FUNCTION app.release_history_deployment(p_deployment text)
RETURNS TABLE (deployment_id text, edition text, operators text[], tenants text[],
    initial_schema integer, allows_test_keys boolean, solo_maintainer boolean,
    recorded_at timestamptz, hash_ok boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$
    SELECT d.deployment_id, d.edition, d.operators,
        ARRAY[app.current_tenant_id()::text], d.initial_schema, d.allows_test_keys,
        d.solo_maintainer, d.recorded_at,
        d.event_hash = encode(sha256(convert_to('["release_deployment",['
            || to_json(d.deployment_id)::text || ',' || to_json(d.edition)::text
            || ',' || (SELECT '[' || coalesce(string_agg(to_json(x)::text, ','
                ORDER BY n), '') || ']' FROM unnest(d.operators)
                WITH ORDINALITY AS u (x, n))
            || ',' || (SELECT '[' || coalesce(string_agg(to_json(x)::text, ','
                ORDER BY n), '') || ']' FROM unnest(d.tenants)
                WITH ORDINALITY AS u (x, n))
            || ',' || d.initial_schema::text || ',' || d.allows_test_keys::text
            || coalesce(',' || d.solo_maintainer::text, '')  -- NULL: before 0032
            || '],' || to_json(to_char(d.recorded_at AT TIME ZONE 'UTC',
                'YYYY-MM-DD"T"HH24:MI:SS') || CASE WHEN to_char(d.recorded_at
                AT TIME ZONE 'UTC', 'US') = '000000' THEN '' ELSE to_char(
                d.recorded_at AT TIME ZONE 'UTC', '.US') END || 'Z')::text
            || ']', 'UTF8')), 'hex')
    FROM app.release_deployment d
    WHERE d.deployment_id = p_deployment
      AND app.current_tenant_id()::text = ANY (d.tenants)
$$;

CREATE FUNCTION app.release_history_events(p_deployment text)
RETURNS SETOF app.deployment_event
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT e.* FROM app.deployment_event e WHERE e.deployment_id = p_deployment
    AND app.release_history_member(p_deployment) ORDER BY e.seq $$;

CREATE FUNCTION app.release_history_releases(p_deployment text)
RETURNS SETOF app.signed_release
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT s.* FROM app.signed_release s
    WHERE app.release_history_member(p_deployment)
      AND s.release_id IN (SELECT e.release_id FROM app.deployment_event e
          WHERE e.deployment_id = p_deployment UNION SELECT e.to_release_id
          FROM app.deployment_event e WHERE e.deployment_id = p_deployment)
    ORDER BY s.release_id $$;

CREATE FUNCTION app.release_history_keys(p_deployment text)
RETURNS SETOF app.release_trusted_key
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT k.* FROM app.release_trusted_key k
    WHERE app.release_history_member(p_deployment) ORDER BY k.key_id $$;

CREATE FUNCTION app.release_history_revocations(p_deployment text)
RETURNS SETOF app.release_key_revocation
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT r.* FROM app.release_key_revocation r
    WHERE app.release_history_member(p_deployment) ORDER BY r.key_id $$;

GRANT EXECUTE ON FUNCTION app.release_history_deployment(text),
    app.release_history_events(text), app.release_history_releases(text),
    app.release_history_keys(text), app.release_history_revocations(text)
    TO qw_app;
