-- T025 increment 5: a worker claims only the job kinds it has handlers for.
--
-- app.job_pick gains the claimer's kinds. The body is 0006's (the latest
-- definition; no later migration redefines it) plus `j.kind = ANY(p_kinds)` in the
-- bucket subquery and the row query. A kind no worker handles stays queued and
-- untouched (attempts, claimed_at, next_available_at unchanged) instead of being
-- claimed and dead-lettered, and it never blocks the jobs behind it. The fairness
-- term (latest claimed_at per tenant and priority) stays kind-blind. The
-- one-argument version is dropped so no caller can claim kind-blind.

DROP FUNCTION app.job_pick(app.job_pool);

CREATE FUNCTION app.job_pick(p_pool app.job_pool, p_kinds text[], OUT tenant_id uuid,
    OUT id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT q.tenant_id, q.priority FROM (
            SELECT DISTINCT j.tenant_id, j.priority FROM app.job j
            WHERE j.pool = p_pool AND j.state = 'queued'
              AND j.next_available_at <= now()
              AND (j.deadline_at IS NULL OR j.deadline_at > now())
              AND j.kind = ANY(p_kinds)) q
        ORDER BY q.priority, (SELECT max(s.claimed_at) FROM app.job s
            WHERE s.pool = p_pool AND s.priority = q.priority
              AND s.tenant_id = q.tenant_id) NULLS FIRST, q.tenant_id
    LOOP
        SELECT j.tenant_id, j.id INTO tenant_id, id FROM app.job j
            WHERE j.tenant_id = r.tenant_id AND j.pool = p_pool
              AND j.priority = r.priority AND j.state = 'queued'
              AND j.next_available_at <= now()
              AND (j.deadline_at IS NULL OR j.deadline_at > now())
              AND j.kind = ANY(p_kinds)
            ORDER BY j.next_available_at, j.created_at, j.id
            LIMIT 1 FOR UPDATE SKIP LOCKED;
        IF FOUND THEN
            RETURN;
        END IF;
    END LOOP;
END
$$;

REVOKE ALL ON FUNCTION app.job_pick(app.job_pool, text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.job_pick(app.job_pool, text[]) TO qw_worker;
