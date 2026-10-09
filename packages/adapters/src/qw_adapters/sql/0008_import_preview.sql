-- 0008: persisted CSV import previews (T014 increment 2; spec 4 "Import workflow";
-- T008 C-03, C-04, F-23). A preview binds a later commit to exactly what was shown:
-- - the uploaded bytes and the request (format, mapping, limits, MIC) as received;
-- - content_hash = SHA-256 over tenant, request and file hash (the commit token is
--   preview_id plus this hash), and result_hash over the derived row outcomes;
-- - expected_revisions {account: revision} the preview was computed against.
-- The commit (qw_adapters.import_store) re-derives the rows under the account locks,
-- so nothing derived is trusted from this table.
-- - Status moves only from 'open' to 'committed', 'expired' or 'superseded'. The
--   other inputs are immutable (trigger, any role), except that a settled preview's
--   raw file may be redacted to NULL (spec 12 retention classes, spec 4 lawful
--   redaction): raw exports can hold personal data. An open preview always keeps its
--   bytes (CHECK). Runtime roles may update only the status and result columns;
--   qw_worker (the retention job) may also update file_bytes. No DELETE or TRUNCATE.
--   The owner and a superuser can bypass triggers, as in 0007.
-- Runtime: previews and commits run as qw_worker (the import job) or qw_app; the
-- commit writes ledger events, so qw_ingest (source records only) gets nothing here.
-- No SECURITY DEFINER function.

CREATE TYPE app.import_preview_status AS ENUM ('open', 'committed', 'expired',
    'superseded');

CREATE TABLE app.import_preview (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    preview_id uuid NOT NULL,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'
        AND octet_length(request::text) <= 65536),
    file_bytes bytea CHECK (octet_length(file_bytes) <= 8388608),  -- NULL: redacted
    expected_revisions jsonb NOT NULL CHECK (jsonb_typeof(expected_revisions) = 'object'
        AND NOT jsonb_path_exists(expected_revisions,
            '$.keyvalue() ? (!(@.key like_regex "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
                || @.value.type() != "number" || @.value < 1)')),
    status app.import_preview_status NOT NULL DEFAULT 'open',
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    committed_at timestamptz,
    posted_count integer CHECK (posted_count >= 0),
    duplicate_count integer CHECK (duplicate_count >= 0),
    PRIMARY KEY (tenant_id, preview_id),
    CHECK (expires_at > created_at),
    CHECK (file_bytes IS NOT NULL OR status <> 'open'),
    CHECK ((status = 'committed') = (committed_at IS NOT NULL)
        AND (committed_at IS NULL) = (posted_count IS NULL)
        AND (committed_at IS NULL) = (duplicate_count IS NULL))
);
CREATE INDEX import_preview_open ON app.import_preview (tenant_id)
    WHERE status = 'open';

CREATE FUNCTION app.import_preview_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF (NEW.tenant_id, NEW.preview_id, NEW.content_hash, NEW.result_hash, NEW.request,
            NEW.expected_revisions, NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM (OLD.tenant_id, OLD.preview_id, OLD.content_hash,
            OLD.result_hash, OLD.request, OLD.expected_revisions, OLD.created_at,
            OLD.expires_at)
       OR (NEW.file_bytes IS NOT NULL AND NEW.file_bytes IS DISTINCT FROM
            OLD.file_bytes) THEN
        RAISE EXCEPTION
            'app.import_preview: inputs are immutable; only status and result change';
    END IF;
    IF (NEW.status, NEW.committed_at, NEW.posted_count, NEW.duplicate_count)
       IS DISTINCT FROM (OLD.status, OLD.committed_at, OLD.posted_count,
            OLD.duplicate_count) THEN
        IF OLD.status <> 'open' OR NEW.status = 'open' THEN
            RAISE EXCEPTION 'app.import_preview: % is settled; % refused',
                OLD.status, NEW.status;
        END IF;
    ELSIF OLD.status = 'open' AND NEW.file_bytes IS NULL THEN
        RAISE EXCEPTION 'app.import_preview: only a settled preview is redacted';
    END IF;
    RETURN NEW;
END
$$;
CREATE TRIGGER status_only BEFORE UPDATE ON app.import_preview
    FOR EACH ROW EXECUTE FUNCTION app.import_preview_guard();
CREATE TRIGGER append_only BEFORE DELETE OR TRUNCATE ON app.import_preview
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.import_preview ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.import_preview FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.import_preview
    USING (tenant_id = app.current_tenant_id());

GRANT USAGE ON TYPE app.import_preview_status TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, preview_id, content_hash, result_hash, request,
    file_bytes, expected_revisions, expires_at),
    UPDATE (status, committed_at, posted_count, duplicate_count)
    ON app.import_preview TO qw_app, qw_worker;
GRANT UPDATE (file_bytes) ON app.import_preview TO qw_worker;
