-- 0012: evidence store and claim graph (T036 increment 3; spec §10; data dictionary
-- evidence_record, claim, claim_evidence), mirroring qw_domain.evidence.Snapshot.
-- qw_adapters.evidence_store writes only what a domain EvidenceStore holds and
-- reloads through EvidenceStore.restore, which re-checks hashes, spans, quote
-- values, revisions and citations. evidence_record is a passage; claim holds
-- revisions; claim_evidence citations ('supports', bound to one revision);
-- evidence_event withdrawals (per origin group), deletion tombstones and retention
-- purges (per source). Append-only: runtime roles get SELECT and column INSERT;
-- statement triggers refuse UPDATE, DELETE and TRUNCATE (as 0007). Only a source's
-- text (evidence_content) can be removed: DELETE is granted there, and a row trigger
-- allows it only after a tombstone, or a purge event once retention has ended by the
-- database clock. No SECURITY DEFINER function: checks run as the caller under RLS.

CREATE TABLE app.evidence_source (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    id text NOT NULL CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    kind text NOT NULL CHECK (kind IN ('primary', 'independent', 'social')),
    feed_id text NOT NULL CHECK (feed_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    feed_hash text NOT NULL CHECK (feed_hash ~ '^[0-9a-f]{64}$'),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    origin text NOT NULL CHECK (origin ~ '^[0-9a-f]{64}$'),
    published_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    retain_until timestamptz NOT NULL CHECK (retain_until >= received_at),
    scope text NOT NULL CHECK (scope IN ('personal', 'commercial')),
    jurisdiction text NOT NULL CHECK (length(jurisdiction) BETWEEN 2 AND 6),
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id)
);

CREATE TABLE app.evidence_content (
    tenant_id uuid NOT NULL,
    source_id text NOT NULL,
    content text NOT NULL CHECK (octet_length(content) <= 1048576),
    PRIMARY KEY (tenant_id, source_id),
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.evidence_source (tenant_id, id)
);

CREATE TABLE app.evidence_record (
    tenant_id uuid NOT NULL,
    id text NOT NULL CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    source_id text NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
    reading jsonb NOT NULL CHECK (jsonb_typeof(reading) = 'object'),
    known_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, source_id) REFERENCES app.evidence_source (tenant_id, id)
);
CREATE INDEX evidence_record_source ON app.evidence_record (tenant_id, source_id, hash);

CREATE TABLE app.claim (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    id text NOT NULL CHECK (id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'),
    revision integer NOT NULL CHECK (revision >= 1),
    kind text NOT NULL CHECK (kind IN ('reported_fact', 'calculation', 'assumption',
                                       'forecast', 'interpretation')),
    stated jsonb NOT NULL CHECK (jsonb_typeof(stated) = 'object'),
    known_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id, revision)
);

CREATE TABLE app.claim_evidence (
    tenant_id uuid NOT NULL,
    id text NOT NULL CHECK (id ~ '^cit-[0-9a-f]{32}$'),
    claim_id text NOT NULL,
    revision integer NOT NULL,
    evidence_id text NOT NULL,
    relation text NOT NULL DEFAULT 'supports' CHECK (relation = 'supports'),
    linked_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    FOREIGN KEY (tenant_id, claim_id, revision)
        REFERENCES app.claim (tenant_id, id, revision),
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES app.evidence_record (tenant_id, id)
);

CREATE TABLE app.claim_resolution (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    position integer NOT NULL CHECK (position >= 0),
    members jsonb NOT NULL CHECK (jsonb_typeof(members) = 'array'),
    preferred text NOT NULL,
    actor_id text NOT NULL,
    at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, position)
);

CREATE TABLE app.evidence_event (
    tenant_id uuid NOT NULL REFERENCES app.tenant (id),
    kind text NOT NULL CHECK (kind IN ('withdrawn', 'deleted', 'purged')),
    subject text NOT NULL,  -- the origin group (withdrawn) or a source id
    at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, kind, subject)
);

CREATE FUNCTION app.evidence_content_drop() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = OLD.tenant_id
               AND e.kind = 'deleted' AND e.subject = OLD.source_id)
       OR (EXISTS (SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = OLD.tenant_id
                   AND e.kind = 'purged' AND e.subject = OLD.source_id)
           AND EXISTS (SELECT 1 FROM app.evidence_source s
                       WHERE s.tenant_id = OLD.tenant_id AND s.id = OLD.source_id
                       AND s.retain_until <= now())) THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'app.evidence_content: % has no tombstone and is retained',
        OLD.source_id USING ERRCODE = 'insufficient_privilege';
END
$$;

-- A passage needs its source's text (its span and value are checked against it); a
-- deleted or purged event a source of this tenant whose text is held (a deletion
-- may follow a purge), dated at or after receipt; a purge, retention ended by the
-- database clock and an `at` not before it; a withdrawal an existing origin group,
-- dated at or after its first receipt. Events cannot leave text-held, retention or
-- receipt inconsistencies; a back-dated event that invalidates existing citations
-- still fails closed at load (as do other forged rows). A source
-- inserted in the same transaction is exempt from the held-text rule: a store may
-- remove text before its first save.
CREATE FUNCTION app.evidence_needs_text() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
DECLARE
    sid text := to_jsonb(NEW) ->> TG_ARGV[0];
    what text := coalesce(to_jsonb(NEW) ->> 'kind', 'passage');
    at timestamptz := (to_jsonb(NEW) ->> 'at')::timestamptz;
    first timestamptz;
    keep timestamptz;
BEGIN
    IF EXISTS (SELECT 1 FROM app.evidence_record r WHERE what = 'passage'
               AND r.tenant_id = NEW.tenant_id AND r.id = to_jsonb(NEW) ->> 'id')
       OR EXISTS (SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = NEW.tenant_id
                  AND e.kind = what AND e.subject = sid) THEN
        RETURN NEW;  -- a repeated row: ON CONFLICT DO NOTHING drops it
    ELSIF what = 'withdrawn' THEN
        SELECT min(s.received_at) INTO first FROM app.evidence_source s
            WHERE s.tenant_id = NEW.tenant_id AND s.origin = sid;
        IF first IS NULL THEN
            RAISE EXCEPTION 'app.evidence_event: no group %', sid
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM app.evidence_content c
                      WHERE c.tenant_id = NEW.tenant_id AND c.source_id = sid)
          AND NOT EXISTS (SELECT 1 FROM app.evidence_source s  -- new in this tx
                          WHERE s.tenant_id = NEW.tenant_id AND s.id = sid
                          AND s.xmin = pg_current_xact_id()::xid)
          AND NOT (what = 'deleted' AND EXISTS (
              SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = NEW.tenant_id
              AND e.kind = 'purged' AND e.subject = sid)) THEN
        RAISE EXCEPTION 'app.%: % has no held text', TG_TABLE_NAME, sid
            USING ERRCODE = 'insufficient_privilege';
    ELSIF what <> 'passage' THEN
        SELECT s.received_at, s.retain_until INTO first, keep FROM app.evidence_source s
            WHERE s.tenant_id = NEW.tenant_id AND s.id = sid;
        IF what = 'purged' AND (keep > now() OR at < keep) THEN
            RAISE EXCEPTION 'app.evidence_event: % is retained until %', sid, keep
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    IF at < first THEN
        RAISE EXCEPTION 'app.evidence_event: % % before receipt', what, sid
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

-- At commit, a deleted or purged source holds no text, and removed text cannot
-- be inserted again: a stored state never has both (restore would refuse it).
CREATE FUNCTION app.evidence_text_removed() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_TABLE_NAME = 'evidence_event' AND (to_jsonb(NEW) ->> 'kind') <> 'withdrawn'
       AND EXISTS (SELECT 1 FROM app.evidence_content c WHERE c.tenant_id = NEW.tenant_id
                   AND c.source_id = to_jsonb(NEW) ->> 'subject') THEN
        RAISE EXCEPTION 'app.evidence_event: % text held after its removal',
            to_jsonb(NEW) ->> 'subject' USING ERRCODE = 'insufficient_privilege';
    ELSIF TG_TABLE_NAME = 'evidence_content' AND EXISTS (
            SELECT 1 FROM app.evidence_event e WHERE e.tenant_id = NEW.tenant_id
            AND e.kind IN ('deleted', 'purged')
            AND e.subject = to_jsonb(NEW) ->> 'source_id') THEN
        RAISE EXCEPTION 'app.evidence_content: % was removed',
            to_jsonb(NEW) ->> 'source_id' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END
$$;

CREATE CONSTRAINT TRIGGER text_gone AFTER INSERT ON app.evidence_event
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.evidence_text_removed();
CREATE TRIGGER not_removed BEFORE INSERT ON app.evidence_content
    FOR EACH ROW EXECUTE FUNCTION app.evidence_text_removed();
CREATE TRIGGER needs_text BEFORE INSERT ON app.evidence_record
    FOR EACH ROW EXECUTE FUNCTION app.evidence_needs_text('source_id');
CREATE TRIGGER needs_text BEFORE INSERT ON app.evidence_event
    FOR EACH ROW EXECUTE FUNCTION app.evidence_needs_text('subject');
CREATE TRIGGER drop_needs_tombstone BEFORE DELETE ON app.evidence_content
    FOR EACH ROW EXECUTE FUNCTION app.evidence_content_drop();
CREATE TRIGGER append_only BEFORE UPDATE OR TRUNCATE ON app.evidence_content
    FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.evidence_source FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.evidence_record FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.claim FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.claim_evidence FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.claim_resolution FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();
CREATE TRIGGER append_only BEFORE UPDATE OR DELETE OR TRUNCATE ON app.evidence_event FOR EACH STATEMENT EXECUTE FUNCTION app.journal_append_only();

ALTER TABLE app.evidence_source ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.evidence_source USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.evidence_content ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.evidence_content USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.evidence_record ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.evidence_record USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.claim ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.claim USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.claim_evidence ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.claim_evidence USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.claim_resolution ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.claim_resolution USING (tenant_id = app.current_tenant_id());
ALTER TABLE app.evidence_event ENABLE ROW LEVEL SECURITY, FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON app.evidence_event USING (tenant_id = app.current_tenant_id());

-- Evidence is recorded by API requests (qw_app) and jobs (qw_worker).
GRANT SELECT, INSERT (tenant_id, id, kind, feed_id, feed_hash, content_hash, origin,
    published_at, received_at, retain_until, scope, jurisdiction)
    ON app.evidence_source TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, source_id, content), DELETE
    ON app.evidence_content TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, id, source_id, start_offset, end_offset, hash,
    reading, known_at) ON app.evidence_record TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, id, revision, kind, stated, known_at)
    ON app.claim TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, id, claim_id, revision, evidence_id, linked_at)
    ON app.claim_evidence TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, position, members, preferred, actor_id, at)
    ON app.claim_resolution TO qw_app, qw_worker;
GRANT SELECT, INSERT (tenant_id, kind, subject, at)
    ON app.evidence_event TO qw_app, qw_worker;
