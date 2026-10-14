-- 0014: virtual portfolios and simulations (T041 increment 3; spec 14, spec 12,
-- R063, R098). Adapter: qw_adapters.simulation_store, which replays the ledger
-- through the domain, re-checks outcomes and fills, and compares the canonical state
-- with the latest simulation_digest row: one changed, deleted or appended row fails
-- closed; rows rewritten together with a matching digest (an owner bypassing
-- triggers, or a runtime role by direct SQL) do not.
-- - simulation: definition and starting capital; simulation_entry: the virtual
--   ledger (capital, seed, fill, dividend, split; options are not persisted);
--   simulation_order: an order and its outcome at the run's as-of (submitted means
--   indeterminate); simulation_fill: each fill is a fill entry; simulation_digest:
--   per save, the SHA-256 of the canonical simulation and its counts.
-- - Separation: a simulation id is never a ledger_event account of its tenant, in
--   both directions (virtual_account_guard). Both triggers first take the journal's
--   per-account lock 'ledger:<tenant>:<account>' (0007), so a concurrent post and
--   simulation cannot both commit. Lock order: tenant simulation lock, then account
--   lock; the journal takes only account locks. Lookups use primary/unique indexes.
-- - virtual, separate_from_actual (true), broker_submission, actual_execution
--   (false) are CHECKed constants. Append-only as 0007: SELECT and column INSERT.

CREATE TABLE app.simulation (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    id text NOT NULL CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    capital numeric(38,12) NOT NULL CHECK (capital > 0),
    execution_model_id text NOT NULL
        CHECK (execution_model_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    benchmark_policy_id text NOT NULL
        CHECK (benchmark_policy_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    mode text NOT NULL CHECK (mode IN ('manual', 'approved_policy_shadow')),
    separate_from_actual boolean NOT NULL DEFAULT true CHECK (separate_from_actual),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE app.simulation_entry (
    tenant_id uuid NOT NULL,
    simulation_id text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    entry_id text NOT NULL CHECK (length(entry_id) BETWEEN 1 AND 300),
    kind text NOT NULL CHECK (kind IN ('capital', 'seed', 'fill', 'dividend', 'split')),
    effective_at timestamptz NOT NULL,
    instrument_id uuid,
    cash numeric(38,12) NOT NULL,
    units numeric(38,12) NOT NULL,
    cost numeric(38,12) NOT NULL,
    realized numeric(38,12) NOT NULL,
    income numeric(38,12) NOT NULL,
    fees numeric(38,12) NOT NULL,
    capital numeric(38,12) NOT NULL,
    ref text NOT NULL CHECK (length(ref) <= 300),
    virtual boolean NOT NULL DEFAULT true CHECK (virtual),
    PRIMARY KEY (tenant_id, simulation_id, position),
    UNIQUE (tenant_id, simulation_id, entry_id),
    FOREIGN KEY (tenant_id, simulation_id) REFERENCES app.simulation (tenant_id, id)
);

CREATE TABLE app.simulation_order (
    tenant_id uuid NOT NULL,
    simulation_id text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    order_id text NOT NULL CHECK (order_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    instrument_id uuid NOT NULL,
    side text NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity numeric(38,12) NOT NULL,
    style text NOT NULL CHECK (style IN ('market', 'limit', 'stop', 'stop_limit')),
    submitted_at timestamptz NOT NULL,
    limit_price numeric(38,12),
    stop_price numeric(38,12),
    expires_at timestamptz,
    proposal_id text,
    oco_group text,
    broker_submission boolean NOT NULL DEFAULT false CHECK (NOT broker_submission),
    evaluated_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('submitted', 'partially_filled', 'filled',
        'unfilled', 'cancelled', 'expired', 'unsupported')),
    remaining numeric(38,12) NOT NULL,
    notes jsonb NOT NULL CHECK (jsonb_typeof(notes) = 'array'),
    PRIMARY KEY (tenant_id, simulation_id, order_id),
    UNIQUE (tenant_id, simulation_id, position),
    FOREIGN KEY (tenant_id, simulation_id) REFERENCES app.simulation (tenant_id, id)
);

CREATE TABLE app.simulation_fill (
    tenant_id uuid NOT NULL,
    simulation_id text NOT NULL,
    fill_id text NOT NULL CHECK (fill_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    order_id text NOT NULL,
    filled_at timestamptz NOT NULL,
    known_at timestamptz NOT NULL,
    bar_start timestamptz NOT NULL,
    quantity numeric(38,12) NOT NULL,
    price numeric(38,12) NOT NULL,
    fees numeric(38,12) NOT NULL,
    execution_model_id text NOT NULL,
    assumptions jsonb NOT NULL CHECK (jsonb_typeof(assumptions) = 'array'),
    ambiguous boolean NOT NULL,
    actual_execution boolean NOT NULL DEFAULT false CHECK (NOT actual_execution),
    PRIMARY KEY (tenant_id, simulation_id, fill_id),
    FOREIGN KEY (tenant_id, simulation_id, order_id)
        REFERENCES app.simulation_order (tenant_id, simulation_id, order_id),
    FOREIGN KEY (tenant_id, simulation_id, fill_id)
        REFERENCES app.simulation_entry (tenant_id, simulation_id, entry_id)
);

CREATE TABLE app.simulation_digest (
    tenant_id uuid NOT NULL,
    simulation_id text NOT NULL,
    position integer NOT NULL CHECK (position >= 0),
    entry_count integer NOT NULL CHECK (entry_count >= 1),
    order_count integer NOT NULL CHECK (order_count >= 0),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, simulation_id, position),
    FOREIGN KEY (tenant_id, simulation_id) REFERENCES app.simulation (tenant_id, id)
);

CREATE FUNCTION app.virtual_account_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    acct text := to_jsonb(NEW) ->> CASE TG_TABLE_NAME WHEN 'simulation' THEN 'id'
                                   ELSE 'account_id' END;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtextextended('ledger:' || NEW.tenant_id || ':' || acct, 0));
    IF (TG_TABLE_NAME = 'simulation' AND EXISTS (SELECT 1 FROM app.ledger_event e
            WHERE e.tenant_id = NEW.tenant_id AND e.account_id = acct))
       OR (TG_TABLE_NAME = 'ledger_event' AND EXISTS (SELECT 1 FROM app.simulation s
            WHERE s.tenant_id = NEW.tenant_id AND s.id = acct)) THEN
        RAISE EXCEPTION 'app.%: % is both a virtual account and a real journal account',
            TG_TABLE_NAME, acct USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER virtual_account_guard BEFORE INSERT ON app.simulation
    FOR EACH ROW EXECUTE FUNCTION app.virtual_account_guard();
CREATE TRIGGER virtual_account_guard BEFORE INSERT ON app.ledger_event
    FOR EACH ROW EXECUTE FUNCTION app.virtual_account_guard();

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.simulation FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.simulation_entry FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.simulation_order FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.simulation_fill FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.simulation_digest FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.simulation ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.simulation USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.simulation_entry ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.simulation_entry USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.simulation_order ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.simulation_order USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.simulation_fill ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.simulation_fill USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.simulation_digest ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.simulation_digest USING (tenant_id = app.current_tenant_id());

-- Simulations are created and run by API requests (qw_app) and jobs (qw_worker).
-- The constant columns are not granted: they always take their default.
GRANT SELECT ON app.simulation, app.simulation_entry, app.simulation_order,
    app.simulation_fill, app.simulation_digest TO qw_app, qw_worker;
GRANT INSERT ON app.simulation_digest TO qw_app, qw_worker;
GRANT INSERT (tenant_id, id, name, currency, capital, execution_model_id,
    benchmark_policy_id, mode) ON app.simulation TO qw_app, qw_worker;
GRANT INSERT (tenant_id, simulation_id, position, entry_id, kind, effective_at,
    instrument_id, cash, units, cost, realized, income, fees, capital, ref)
    ON app.simulation_entry TO qw_app, qw_worker;
GRANT INSERT (tenant_id, simulation_id, position, order_id, instrument_id, side,
    quantity, style, submitted_at, limit_price, stop_price, expires_at, proposal_id,
    oco_group, evaluated_at, status, remaining, notes)
    ON app.simulation_order TO qw_app, qw_worker;
GRANT INSERT (tenant_id, simulation_id, fill_id, order_id, filled_at, known_at,
    bar_start, quantity, price, fees, execution_model_id, assumptions, ambiguous)
    ON app.simulation_fill TO qw_app, qw_worker;
