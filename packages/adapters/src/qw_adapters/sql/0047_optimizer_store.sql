-- Optimizer experiments, cycles and lineage (T050; owner decision 87, 2026-10-24,
-- "New installation tables": qw_assessor writes the registry, qw_worker writes
-- cycles, only maintainers/operators read them). Installation scope, as 0029: no
-- tenant id and no tenant policy; forced RLS with per-role policies, and qw_app,
-- qw_ingest and PUBLIC hold nothing. Readers: the operator's qw_release login
-- (decision 45); no maintainer login exists yet (routes.py: no installation
-- maintainer role has a table, assignment or session field), so none is granted.
-- Every row carries a store-computed `event_hash`, re-verified on every read
-- (qw_adapters.optimizer_store), and a DB-clock `recorded_at` (60 s back, never
-- ahead). Append-only for every role and the owner. The guards run as the owner
-- (qw_migrate, SECURITY DEFINER, fixed search_path, no EXECUTE grant, as 0044), so
-- a blind writer needs no SELECT; qw_migrate holds SELECT-only policies for them.
-- - `optimizer_registry_entry`: the optimizer experiments' protocol, candidate and
--   verdict entries, hash-chained per installation (0044's chain: seq n + 1, the
--   head's hash, time never backwards, under the `optimizer:<installation>` lock).
--   qw_assessor alone writes every kind and reads it (it replays the chain through
--   the domain before each append, as 0044's assessor does); qw_worker, which runs
--   untrusted experiments, holds nothing (decision 79's separation, kept whole).
-- - `optimizer_cycle`: the strict `CycleReceipt` wire (exact key set, JSON types,
--   canonical UTC `at`), written by qw_worker without SELECT under a host-issued
--   `cycle_ref`; qw_assessor reads it (decision 91). Refused: a duplicate cycle id
--   or receipt in the installation; a
--   receipt time later than `recorded_at`, more than 60 s before it, or before the
--   experiment's latest stored cycle (`back_dated`); and any runner but the
--   logged protocol's frozen baseline (`runner_not_baseline`: hash, version and
--   `baseline:<version>` authority). LIMITATION: an installed V1 cannot be stored
--   yet; 0029 refuses an optimizer release (R076), so no install can be checked.
-- - `optimizer_lineage`: verified lineage receipts, written blind by qw_assessor
--   (the independent party that runs `verify_lineage`; the worker would certify
--   its own recursion). Each cites a logged protocol and verdict of its experiment
--   and candidate (`not_logged`). Its cycles are not required to be stored (V1's
--   cannot be yet); a load is unverified until `verify_lineage` re-derives it.

CREATE TABLE app.optimizer_registry_entry (
    installation_id text NOT NULL
        CHECK (installation_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    seq bigint NOT NULL CHECK (seq >= 1),
    kind text NOT NULL CHECK (kind IN ('protocol', 'candidate', 'verdict')),
    entry_id text NOT NULL
        CHECK (entry_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    body jsonb NOT NULL,
    recorded_at timestamptz NOT NULL,
    prev_hash text CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (installation_id, seq),
    UNIQUE (installation_id, kind, entry_id),
    CHECK (body ->> CASE kind WHEN 'protocol' THEN 'experiment_id'
        WHEN 'candidate' THEN 'candidate_id' ELSE 'verdict_id' END = entry_id),
    CHECK (kind <> 'protocol' OR (body ->> 'target' = 'recursive_optimizer'
        AND body ->> 'scope' = 'installation'
        AND body ->> 'scope_id' = installation_id))
);

CREATE TABLE app.optimizer_cycle (
    cycle_ref uuid PRIMARY KEY,
    installation_id text NOT NULL,
    experiment_id text NOT NULL,
    cycle_id text NOT NULL,
    runner_hash text NOT NULL CHECK (runner_hash ~ '^[0-9a-f]{64}$'),
    authority text NOT NULL,
    body jsonb NOT NULL,
    receipt_hash text NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE (installation_id, cycle_id),
    UNIQUE (installation_id, receipt_hash),
    CHECK (body ->> 'cycle_id' = cycle_id AND body ->> 'runner_hash' = runner_hash
        AND body ->> 'authority' = authority
        AND body ->> 'receipt_hash' = receipt_hash),
    -- the CycleReceipt wire's exact key set and JSON types, and its canonical
    -- UTC instant (`format_instant`), so a raw qw_worker insert cannot carry a
    -- junk body or an instant read under the session's TimeZone or DateStyle
    CONSTRAINT optimizer_cycle_wire CHECK (jsonb_typeof(body) = 'object'
        AND body ?& ARRAY['at', 'authority', 'base_hash', 'config', 'cycle_id',
                    'declared_cost', 'fixture_hash', 'outputs_hash', 'proposals',
                    'receipt_hash', 'runner_hash', 'runner_version', 'selected']
        AND body - ARRAY['at', 'authority', 'base_hash', 'config', 'cycle_id',
                    'declared_cost', 'fixture_hash', 'outputs_hash', 'proposals',
                    'receipt_hash', 'runner_hash', 'runner_version', 'selected']
            = '{}'::jsonb
        AND jsonb_typeof(body -> 'config') = 'object'
        AND jsonb_typeof(body -> 'proposals') = 'array'
        AND jsonb_typeof(body -> 'selected') IN ('string', 'null')
        AND jsonb_typeof(body -> 'at') = 'string'
        AND jsonb_typeof(body -> 'authority') = 'string'
        AND jsonb_typeof(body -> 'base_hash') = 'string'
        AND jsonb_typeof(body -> 'cycle_id') = 'string'
        AND jsonb_typeof(body -> 'declared_cost') = 'string'
        AND jsonb_typeof(body -> 'fixture_hash') = 'string'
        AND jsonb_typeof(body -> 'outputs_hash') = 'string'
        AND jsonb_typeof(body -> 'runner_version') = 'string'
        AND body ->> 'at'
            ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?Z$')
);

CREATE TABLE app.optimizer_lineage (
    installation_id text NOT NULL,
    lineage_id text NOT NULL,
    experiment_id text NOT NULL,
    body jsonb NOT NULL,
    lineage_hash text NOT NULL CHECK (lineage_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL,
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (installation_id, lineage_id),
    UNIQUE (installation_id, lineage_hash),
    CHECK (body ->> 'lineage_id' = lineage_id
        AND body ->> 'experiment_id' = experiment_id
        AND body ->> 'lineage_hash' = lineage_hash)
);

CREATE FUNCTION app.optimizer_registry_entry_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    p app.optimizer_registry_entry%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'optimizer:' || NEW.installation_id, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.optimizer_registry_entry: clock'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO p FROM app.optimizer_registry_entry
        WHERE installation_id = NEW.installation_id ORDER BY seq DESC LIMIT 1;
    IF NEW.seq <> coalesce(p.seq, 0) + 1
       OR NEW.prev_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at THEN
        RAISE EXCEPTION 'app.optimizer_registry_entry: registry_moved'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION app.optimizer_cycle_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
DECLARE
    -- only the canonical UTC form is read, as UTC whatever the session's TimeZone
    -- or DateStyle; anything else stays NULL and optimizer_cycle_wire refuses it
    ts timestamptz := CASE WHEN NEW.body ->> 'at' ~ ('^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
        || '[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?Z$')
        THEN left(NEW.body ->> 'at', -1)::timestamp AT TIME ZONE 'UTC' END;
    latest timestamptz;
    p jsonb;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'optimizer_cycle:' || NEW.installation_id, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.optimizer_cycle: clock' USING ERRCODE = 'check_violation';
    END IF;
    SELECT max(left(c.body ->> 'at', -1)::timestamp AT TIME ZONE 'UTC') INTO latest
        FROM app.optimizer_cycle c
        WHERE c.installation_id = NEW.installation_id
          AND c.experiment_id = NEW.experiment_id;
    IF ts > NEW.recorded_at OR ts < NEW.recorded_at - interval '60 seconds'
       OR ts < latest THEN
        RAISE EXCEPTION 'app.optimizer_cycle: back_dated'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT e.body INTO p FROM app.optimizer_registry_entry e
        WHERE e.installation_id = NEW.installation_id AND e.kind = 'protocol'
          AND e.entry_id = NEW.experiment_id;
    IF p IS NULL OR NEW.runner_hash IS DISTINCT FROM p ->> 'baseline_hash'
       OR NEW.body ->> 'runner_version' IS DISTINCT FROM p ->> 'baseline_version'
       OR NEW.authority IS DISTINCT FROM 'baseline:' || (p ->> 'baseline_version') THEN
        RAISE EXCEPTION 'app.optimizer_cycle: runner_not_baseline'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE FUNCTION app.optimizer_lineage_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.optimizer_lineage: clock'
            USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.optimizer_registry_entry e
           WHERE e.installation_id = NEW.installation_id AND e.kind = 'protocol'
             AND e.entry_id = NEW.experiment_id)
       OR NOT EXISTS (SELECT 1 FROM app.optimizer_registry_entry e
           WHERE e.installation_id = NEW.installation_id AND e.kind = 'verdict'
             AND e.entry_id = NEW.body ->> 'verdict_id'
             AND e.body ->> 'experiment_id' = NEW.experiment_id
             AND e.body ->> 'candidate_id' = NEW.body ->> 'candidate_id') THEN
        RAISE EXCEPTION 'app.optimizer_lineage: not_logged'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['optimizer_registry_entry', 'optimizer_cycle',
                             'optimizer_lineage'] LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION app.%I() FROM PUBLIC', t || '_guard');
        EXECUTE format('CREATE TRIGGER insert_guard BEFORE INSERT ON app.%I '
            'FOR EACH ROW EXECUTE FUNCTION app.%I()', t, t || '_guard');
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR '
            'TRUNCATE ON app.%I FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.journal_append_only()', t);
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY, '
            'FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('CREATE POLICY installation_operator ON app.%I FOR SELECT '
            'TO qw_release USING (true)', t);
        EXECUTE format('CREATE POLICY installation_guard ON app.%I FOR SELECT '
            'TO qw_migrate USING (true)', t);
        EXECUTE format('GRANT SELECT ON app.%I TO qw_release', t);
    END LOOP;
END
$$;

CREATE POLICY installation_assessor ON app.optimizer_registry_entry TO qw_assessor
    USING (true) WITH CHECK (true);
CREATE POLICY installation_worker ON app.optimizer_cycle FOR INSERT TO qw_worker
    WITH CHECK (true);
CREATE POLICY installation_assessor ON app.optimizer_cycle FOR SELECT TO qw_assessor
    USING (true);  -- owner decision 91
CREATE POLICY installation_assessor ON app.optimizer_lineage FOR INSERT
    TO qw_assessor WITH CHECK (true);

GRANT SELECT ON app.optimizer_registry_entry, app.optimizer_cycle TO qw_assessor;
GRANT INSERT (installation_id, seq, kind, entry_id, body, recorded_at, prev_hash,
    event_hash) ON app.optimizer_registry_entry TO qw_assessor;
GRANT INSERT (cycle_ref, installation_id, experiment_id, cycle_id, runner_hash,
    authority, body, receipt_hash, recorded_at, event_hash)
    ON app.optimizer_cycle TO qw_worker;
GRANT INSERT (installation_id, lineage_id, experiment_id, body, lineage_hash,
    recorded_at, event_hash) ON app.optimizer_lineage TO qw_assessor;
