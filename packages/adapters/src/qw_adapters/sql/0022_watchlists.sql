-- 0022: watchlists (T024 increment 2; contract listWatchlists, createWatchlist,
-- readWatchlist, updateWatchlist, deleteWatchlist; spec §2 Journey C, §13).
-- Adapter: qw_adapters.watchlist_store, which re-hashes every revision, checks each
-- chain and replays it through qw_domain.watchlists.WatchlistStore, failing closed.
-- - One append-only revision per change, each a full snapshot (name, description,
--   entries): revision = previous + 1 from 1 (the create), each naming the previous
--   revision's hash. 'delete' is a final tombstone; nothing follows it.
-- - entries is a JSON array of the contract WatchlistEntry; it stays empty until
--   the entry operations exist (the adapter refuses anything else).
-- - Research state only: no reference to accounts, sources, the journal or holdings.
-- - recorded_at is the DB clock (at most 60 s back, never ahead), never before the
--   previous revision; the principal's own live session. Inserts serialise on
--   'watchlist:<tenant>', taken after every other advisory lock when held with one.
-- - qw_app: SELECT and column INSERT. qw_worker: nothing.

CREATE TABLE app.watchlist_revision (
    tenant_id uuid NOT NULL,
    watchlist_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision BETWEEN 1 AND 2147483646),
    action text NOT NULL CHECK (action IN ('create', 'update', 'delete')),
    principal_id uuid NOT NULL,
    session_id uuid NOT NULL,
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{16,128}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
    description text NOT NULL CHECK (char_length(description) <= 4000),
    entries jsonb NOT NULL CHECK (jsonb_typeof(entries) = 'array'),
    recorded_at timestamptz NOT NULL,
    previous_hash text CHECK (previous_hash ~ '^[0-9a-f]{64}$'),
    event_hash text NOT NULL CHECK (event_hash ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (tenant_id, watchlist_id, revision),
    UNIQUE (tenant_id, watchlist_id, principal_id, action, idempotency_key),
    CHECK ((revision = 1) = (previous_hash IS NULL)),
    CHECK ((revision = 1) = (action = 'create')),
    FOREIGN KEY (tenant_id, principal_id) REFERENCES app.membership (tenant_id, user_id),
    FOREIGN KEY (tenant_id, session_id) REFERENCES app.session (tenant_id, id)
);

CREATE UNIQUE INDEX watchlist_revision_create_key ON app.watchlist_revision
    (tenant_id, principal_id, idempotency_key) WHERE revision = 1;

CREATE FUNCTION app.watchlist_revision_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    p record;
    s record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'watchlist:' || NEW.tenant_id::text, 0));
    IF NEW.recorded_at > clock_timestamp()
       OR NEW.recorded_at < clock_timestamp() - interval '60 seconds' THEN
        RAISE EXCEPTION 'app.watchlist_revision: % is not the database clock',
            NEW.recorded_at USING ERRCODE = 'check_violation';
    END IF;
    SELECT revision, action, recorded_at, event_hash INTO p
        FROM app.watchlist_revision
        WHERE tenant_id = NEW.tenant_id AND watchlist_id = NEW.watchlist_id
        ORDER BY revision DESC LIMIT 1;
    IF NEW.revision <> coalesce(p.revision, 0) + 1
       OR NEW.previous_hash IS DISTINCT FROM p.event_hash
       OR NEW.recorded_at < p.recorded_at OR p.action = 'delete' THEN
        RAISE EXCEPTION 'app.watchlist_revision: not the next revision'
            USING ERRCODE = 'check_violation';
    END IF;
    SELECT user_id, revoked_at, expires_at INTO s FROM app.session
        WHERE tenant_id = NEW.tenant_id AND id = NEW.session_id;
    IF NOT FOUND OR s.user_id <> NEW.principal_id OR s.revoked_at IS NOT NULL
       OR s.expires_at <= now() THEN
        RAISE EXCEPTION 'app.watchlist_revision: not the principal''s live session'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER insert_guard BEFORE INSERT ON app.watchlist_revision
    FOR EACH ROW EXECUTE FUNCTION app.watchlist_revision_guard();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE
    ON app.watchlist_revision
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.watchlist_revision ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.watchlist_revision
    USING (tenant_id = app.current_tenant_id());

GRANT SELECT, INSERT (tenant_id, watchlist_id, revision, action, principal_id,
    session_id, idempotency_key, name, description, entries, recorded_at,
    previous_hash, event_hash) ON app.watchlist_revision TO qw_app;
