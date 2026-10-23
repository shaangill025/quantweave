-- Release history reads with explicit columns (project contract 0.13.0; the
-- cross-model review's Info note on 0037; owner decision 63 of 2026-10-23).
-- 0037's member-only SECURITY DEFINER reads returned whole table rows (SETOF the
-- table type), so qw_app received columns the read never uses, and would silently
-- receive any column a later migration adds. They are replaced by functions with
-- the same names, arguments, owner (qw_migrate), search_path and membership rule
-- that return explicit columns: what the store hashes and replays, nothing more.
-- `release_history_releases` drops the columns the CHECKs of 0029 bind to the
-- signed body (component, target, candidate_id, candidate_hash, verdict_id,
-- approver_id, min_schema, max_schema, writes_schema); the store rebuilds them from
-- the body. A changed return type needs DROP and CREATE, which resets the grants,
-- so EXECUTE is granted to qw_app again. `release_history_deployment` already
-- returned explicit columns and is unchanged.

DROP FUNCTION app.release_history_events(text), app.release_history_releases(text),
    app.release_history_keys(text), app.release_history_revocations(text);

CREATE FUNCTION app.release_history_events(p_deployment text)
RETURNS TABLE (deployment_id text, seq integer, event_id text, kind text,
    component text, release_id text, to_release_id text, reasons text[],
    operator_id text, step_up_receipt_id text, at timestamptz, schema_after integer,
    self_approved boolean, event_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT e.deployment_id, e.seq, e.event_id, e.kind, e.component, e.release_id,
    e.to_release_id, e.reasons, e.operator_id, e.step_up_receipt_id, e.at,
    e.schema_after, e.self_approved, e.event_hash
    FROM app.deployment_event e WHERE e.deployment_id = p_deployment
    AND app.release_history_member(p_deployment) ORDER BY e.seq $$;

CREATE FUNCTION app.release_history_releases(p_deployment text)
RETURNS TABLE (release_id text, manifest_hash text, key_id text, algorithm text,
    signature text, body jsonb, approved_at timestamptz, verdict_source text,
    recorded_at timestamptz, event_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT s.release_id, s.manifest_hash, s.key_id, s.algorithm, s.signature,
    s.body, s.approved_at, s.verdict_source, s.recorded_at, s.event_hash
    FROM app.signed_release s
    WHERE app.release_history_member(p_deployment)
      AND s.release_id IN (SELECT e.release_id FROM app.deployment_event e
          WHERE e.deployment_id = p_deployment UNION SELECT e.to_release_id
          FROM app.deployment_event e WHERE e.deployment_id = p_deployment)
    ORDER BY s.release_id $$;

CREATE FUNCTION app.release_history_keys(p_deployment text)
RETURNS TABLE (key_id text, principal_id text, algorithm text, public_key text,
    test_only boolean, valid_from timestamptz, valid_until timestamptz,
    recorded_at timestamptz, event_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT k.key_id, k.principal_id, k.algorithm, k.public_key, k.test_only,
    k.valid_from, k.valid_until, k.recorded_at, k.event_hash
    FROM app.release_trusted_key k
    WHERE app.release_history_member(p_deployment) ORDER BY k.key_id $$;

CREATE FUNCTION app.release_history_revocations(p_deployment text)
RETURNS TABLE (key_id text, reason text, revoked_at timestamptz, event_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog
AS $$ SELECT r.key_id, r.reason, r.revoked_at, r.event_hash
    FROM app.release_key_revocation r
    WHERE app.release_history_member(p_deployment) ORDER BY r.key_id $$;

GRANT EXECUTE ON FUNCTION app.release_history_events(text),
    app.release_history_releases(text), app.release_history_keys(text),
    app.release_history_revocations(text) TO qw_app;
