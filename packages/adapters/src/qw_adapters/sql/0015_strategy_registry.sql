-- 0015: strategy registry (T027 persistence; spec §8 manifests and qualification,
-- §11/§16 adoption, §3 global versus tenant data, T008 C-20/C-22). Mirrors
-- qw_domain.strategy_registry; the adapter is qw_adapters.strategy_store, which
-- rebuilds every manifest through the domain constructors, requires its stored body
-- to be canonical and its hashes to match, and replays adoptions through
-- `StrategyRegistry`, so a forged row fails closed.
-- - Global (installation) facts: strategy_manifest. A manifest version is code
--   shipped with the installation, the same for every tenant: runtime roles read it,
--   only the schema owner qw_migrate (operator-held; future release tooling) inserts.
--   RLS stays forced with an open read policy, as 0013.
-- - Tenant facts: strategy_adoption (C-20 `user_strategy_adoption` receipts), under
--   the forced tenant isolation policy. Runtime roles read only: the adopt route,
--   which must verify step-up (T010) and If-Match, is not served yet; until then
--   only the owner inserts, inside a tenant transaction.
-- - Not persisted yet: family policies and qualification steps (operational and
--   evidence axes are installation facts keyed on material and configuration
--   hashes; eligibility is per tenant). Without them every manifest reads as not
--   tested, no evidence, not evaluated.
-- - Append-only: a new manifest version or a new adoption receipt is a new row.
--   Statement triggers refuse UPDATE, DELETE and TRUNCATE, for the owner too.
-- - signed_at follows the database clock (at most 60 seconds back, never in the
--   future, never before the tenant's previous receipt for that strategy).
-- Nothing is seeded: the six reference manifests need feed ids, an ai_thesis model
-- config and a release decision that no store holds (docs/receipts/T027.md).

CREATE TABLE app.strategy_manifest (
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    strategy_id text NOT NULL
        CHECK (strategy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    version text NOT NULL CHECK (version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    family text NOT NULL CHECK (family IN ('allocation', 'quality_value', 'income',
        'swing_momentum', 'opening_range_breakout', 'ai_thesis')),
    body jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object'
        AND octet_length(body::text) <= 65536),
    material_hash text NOT NULL CHECK (material_hash ~ '^[0-9a-f]{64}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (strategy_id, version),
    UNIQUE (strategy_id, version, material_hash)
);

CREATE TABLE app.strategy_adoption (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    strategy_id text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    version text NOT NULL,
    manifest_hash text NOT NULL,
    config_hash text NOT NULL CHECK (config_hash ~ '^[0-9a-f]{64}$'),
    principal_id text NOT NULL
        CHECK (principal_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    step_up_receipt_id text NOT NULL
        CHECK (step_up_receipt_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    signed_at timestamptz NOT NULL,
    acknowledgement_text text NOT NULL CHECK (length(acknowledgement_text) <= 2000),
    receipt_hash text NOT NULL CHECK (receipt_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, strategy_id, position),
    FOREIGN KEY (strategy_id, version, manifest_hash)
        REFERENCES app.strategy_manifest (strategy_id, version, material_hash)
);

CREATE FUNCTION app.strategy_signed_now() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    last timestamptz;
BEGIN
    IF NEW.signed_at > clock_timestamp()
        OR NEW.signed_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.strategy_adoption: signed_at % is not the database clock',
            NEW.signed_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT max(a.signed_at) INTO last FROM app.strategy_adoption a
        WHERE a.tenant_id = NEW.tenant_id AND a.strategy_id = NEW.strategy_id;
    IF NEW.signed_at < last THEN
        RAISE EXCEPTION 'app.strategy_adoption: signed_at % is before the previous row (%)',
            NEW.signed_at, last USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER signed_now BEFORE INSERT ON app.strategy_adoption
    FOR EACH ROW EXECUTE FUNCTION app.strategy_signed_now();

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.strategy_manifest FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.strategy_adoption FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.strategy_manifest ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY global_read ON app.strategy_manifest FOR SELECT USING (true);
CREATE POLICY owner_insert ON app.strategy_manifest FOR INSERT TO qw_migrate WITH CHECK (true);
ALTER TABLE app.strategy_adoption ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.strategy_adoption USING (tenant_id = app.current_tenant_id());

GRANT SELECT ON app.strategy_manifest, app.strategy_adoption TO qw_app, qw_worker;
