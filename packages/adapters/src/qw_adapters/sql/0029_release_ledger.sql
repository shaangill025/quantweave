-- 0029: T051 release ledger (spec §11 promotion, adoption and recovery, §15
-- rollback; data dictionary `release_receipt`, scope installation), written as
-- qw_worker through qw_adapters.release_store and replayed through the domain
-- `DeploymentLedger` on every read (fail closed).
-- Scope: installation, not tenant. A signed release is shared by every deployment
-- that installs it, and a deployment's events are operator acts; neither carries
-- a tenant id, so RLS stays forced with a qw_worker-only policy (no open read) and
-- qw_app has no privilege at all: the deployment's tenant list is cross-tenant
-- data. Tenant adoptions stay in 0015 `strategy_adoption` under tenant isolation;
-- nothing here names a tenant's adoption.
-- Append-only. Enforced here: a release is never a code or optimizer target
-- (R076) and requires maintainer release; its body matches its columns; a
-- deployment's events are seq 1..n in time order on the DB clock (60 s back, never
-- ahead), under lock 'release:<deployment>'; a release is installed once per
-- deployment by a listed operator who did not sign it, not before its approval,
-- on a schema inside its range; the state schema only moves forward
-- (schema_after = max(before, writes) on install, unchanged otherwise); rollback
-- and pause act only on the component's current head, and rollback only to an
-- earlier, not withdrawn or paused, compatible install of the same component; a step-up
-- authorises one act per deployment, the signer's approval included (no event
-- reuses an installed release's approval step-up, and no install reuses an event's
-- or another installed release's); deployment members are sorted sets.
-- DB-accepted but domain-refused (replay then reads `integrity`): signatures, key
-- trust, validity and revocation, the test-key policy, step-up reuse with tenant
-- adoptions, and event hashes (no SQL hashing in 0026-0028, so no extension is
-- added: a raw qw_worker row with a wrong hash makes the deployment unreadable
-- until the owner repairs it). Keys are the operator's and never stored.

CREATE TABLE app.release_deployment (
    deployment_id text PRIMARY KEY
        CHECK (deployment_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    edition text NOT NULL CHECK (edition IN ('hosted', 'self_hosted')),
    operators text[] NOT NULL CHECK (cardinality(operators) BETWEEN 1 AND 256),
    tenants text[] NOT NULL CHECK (cardinality(tenants) <= 100000),
    initial_schema integer NOT NULL CHECK (initial_schema >= 1),
    allows_test_keys boolean NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE app.signed_release (
    release_id text PRIMARY KEY
        CHECK (release_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    component text NOT NULL,
    target text NOT NULL
        CHECK (target IN ('personal_adaptation', 'research', 'strategy', 'evaluator')),
    candidate_id text NOT NULL,
    candidate_hash text NOT NULL CHECK (candidate_hash ~ '^[0-9a-f]{64}$'),
    verdict_id text NOT NULL,
    manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
    approver_id text NOT NULL,
    approved_at timestamptz NOT NULL,
    min_schema integer NOT NULL,
    max_schema integer NOT NULL,
    writes_schema integer NOT NULL,
    key_id text NOT NULL,
    algorithm text NOT NULL,
    signature text NOT NULL CHECK (char_length(signature) BETWEEN 1 AND 4096),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    CHECK (1 <= min_schema AND min_schema <= writes_schema
        AND writes_schema <= max_schema),
    CHECK (split_part(component, ':', 1) = target
        AND split_part(component, ':', 2) <> ''
        AND (target = 'strategy' OR component = target || ':' || target)),
    CHECK (body ->> 'release_id' = release_id AND body ->> 'component' = component
        AND body ->> 'target' = target AND body ->> 'candidate_id' = candidate_id
        AND body ->> 'candidate_hash' = candidate_hash
        AND body ->> 'verdict_id' = verdict_id
        AND body -> 'compatibility'
            = jsonb_build_array(min_schema, max_schema, writes_schema)
        AND body -> 'approval' ->> 0 = approver_id
        AND body -> 'required_approvals' ? 'maintainer_release'),
    CHECK (approved_at <= recorded_at)
);
CREATE INDEX signed_release_candidate ON app.signed_release (candidate_hash);

CREATE TABLE app.deployment_event (
    deployment_id text NOT NULL REFERENCES app.release_deployment (deployment_id),
    seq integer NOT NULL CHECK (seq BETWEEN 1 AND 2147483646),
    event_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('install', 'rollback', 'pause')),
    component text NOT NULL,
    release_id text NOT NULL REFERENCES app.signed_release (release_id),
    to_release_id text REFERENCES app.signed_release (release_id),
    reasons text[] NOT NULL CHECK (cardinality(reasons) <= 256),
    operator_id text NOT NULL,
    step_up_receipt_id text NOT NULL,
    at timestamptz NOT NULL,
    schema_after integer NOT NULL CHECK (schema_after >= 1),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (deployment_id, seq),
    UNIQUE (deployment_id, event_id),
    UNIQUE (deployment_id, step_up_receipt_id),
    CHECK ((kind = 'rollback') = (to_release_id IS NOT NULL)),
    CHECK ((kind = 'pause') = (cardinality(reasons) > 0))
);
-- one install of a release id per deployment (R092 release identity)
CREATE UNIQUE INDEX deployment_event_one_install
    ON app.deployment_event (deployment_id, release_id) WHERE kind = 'install';

CREATE FUNCTION app.release_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    d app.release_deployment%ROWTYPE;
    r app.signed_release%ROWTYPE;
    p app.deployment_event%ROWTYPE;
    ts timestamptz;
    before integer;
    head text;
    ok boolean;
BEGIN
    IF TG_TABLE_NAME = 'deployment_event' THEN
        ts := NEW.at;
    ELSE
        ts := NEW.recorded_at;
    END IF;
    IF ts > clock_timestamp() OR ts < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.%: % is not the database clock', TG_TABLE_NAME, ts
            USING ERRCODE = 'check_violation';
    END IF;
    IF TG_TABLE_NAME = 'release_deployment' THEN  -- the store's sorted sets
        IF NEW.operators IS DISTINCT FROM ARRAY(SELECT DISTINCT x COLLATE "C"
               FROM unnest(NEW.operators) AS u (x) ORDER BY 1)
           OR NEW.tenants IS DISTINCT FROM ARRAY(SELECT DISTINCT x COLLATE "C"
               FROM unnest(NEW.tenants) AS u (x) ORDER BY 1) THEN
            RAISE EXCEPTION 'app.release_deployment: members are not a sorted set'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF TG_TABLE_NAME <> 'deployment_event' THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'release:' || NEW.deployment_id, 0));
    SELECT * INTO d FROM app.release_deployment
        WHERE deployment_id = NEW.deployment_id;
    SELECT * INTO p FROM app.deployment_event WHERE deployment_id = NEW.deployment_id
        ORDER BY seq DESC LIMIT 1;
    before := coalesce(p.schema_after, d.initial_schema);
    SELECT CASE kind WHEN 'rollback' THEN to_release_id ELSE release_id END
        INTO head FROM app.deployment_event
        WHERE deployment_id = NEW.deployment_id AND component = NEW.component
        ORDER BY seq DESC LIMIT 1;
    ok := NEW.seq = coalesce(p.seq, 0) + 1 AND NEW.at >= coalesce(p.at, NEW.at)
        AND NEW.operator_id = ANY (d.operators)
        AND NOT EXISTS (SELECT 1 FROM app.deployment_event e  -- a signer's step-up
            JOIN app.signed_release s ON s.release_id = e.release_id
            WHERE e.deployment_id = NEW.deployment_id AND e.kind = 'install'
              AND s.body -> 'approval' ->> 1 = NEW.step_up_receipt_id);
    IF NEW.kind = 'install' THEN
        SELECT * INTO r FROM app.signed_release WHERE release_id = NEW.release_id;
        ok := ok AND r.component = NEW.component AND NEW.at >= r.approved_at
            AND NEW.operator_id <> r.approver_id
            AND before BETWEEN r.min_schema AND r.max_schema
            AND NEW.schema_after = greatest(before, r.writes_schema)
            AND r.body -> 'approval' ->> 1 <> NEW.step_up_receipt_id
            AND NOT EXISTS (SELECT 1 FROM app.deployment_event e
                WHERE e.deployment_id = NEW.deployment_id
                  AND e.step_up_receipt_id = r.body -> 'approval' ->> 1)
            AND NOT EXISTS (SELECT 1 FROM app.deployment_event e
                JOIN app.signed_release s ON s.release_id = e.release_id
                WHERE e.deployment_id = NEW.deployment_id AND e.kind = 'install'
                  AND s.release_id <> r.release_id  -- a re-install: the index
                  AND s.body -> 'approval' ->> 1 = r.body -> 'approval' ->> 1);
    ELSE
        ok := ok AND NEW.release_id = head AND NEW.schema_after = before;
        IF NEW.kind = 'rollback' THEN
            SELECT * INTO r FROM app.signed_release WHERE release_id = NEW.to_release_id;
            ok := ok AND NEW.to_release_id <> NEW.release_id
                AND r.component = NEW.component
                AND before BETWEEN r.min_schema AND r.max_schema
                AND EXISTS (SELECT 1 FROM app.deployment_event
                    WHERE deployment_id = NEW.deployment_id AND kind = 'install'
                      AND release_id = NEW.to_release_id)
                AND NOT EXISTS (SELECT 1 FROM app.deployment_event
                    WHERE deployment_id = NEW.deployment_id
                      AND kind IN ('rollback', 'pause')
                      AND release_id = NEW.to_release_id);  -- withdrawn or paused
        END IF;
    END IF;
    IF ok IS NOT TRUE THEN
        RAISE EXCEPTION 'app.deployment_event: not the next event of %',
            NEW.deployment_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['release_deployment', 'signed_release',
                             'deployment_event'] LOOP
        EXECUTE format('CREATE TRIGGER insert_guard BEFORE INSERT ON app.%I '
            'FOR EACH ROW EXECUTE FUNCTION app.release_guard()', t);
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR '
            'TRUNCATE ON app.%I FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.journal_append_only()', t);
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY, '
            'FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY installation_worker ON app.%I TO qw_worker '
            'USING (true) WITH CHECK (true)', t);
        EXECUTE format('GRANT SELECT ON app.%I TO qw_worker', t);
    END LOOP;
END
$$;

GRANT INSERT (deployment_id, edition, operators, tenants, initial_schema,
    allows_test_keys, recorded_at, event_hash) ON app.release_deployment TO qw_worker;
GRANT INSERT (release_id, component, target, candidate_id, candidate_hash,
    verdict_id, manifest_hash, approver_id, approved_at, min_schema, max_schema,
    writes_schema, key_id, algorithm, signature, body, recorded_at, event_hash)
    ON app.signed_release TO qw_worker;
GRANT INSERT (deployment_id, seq, event_id, kind, component, release_id,
    to_release_id, reasons, operator_id, step_up_receipt_id, at, schema_after,
    event_hash) ON app.deployment_event TO qw_worker;
