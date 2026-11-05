-- Owner decision 136 (T058 increment 9): a deletion request's acknowledgement is
-- kept as the record of what the tenant owner agreed to, with the requested
-- classes, so the record outlives the job's purge window. 0002's audit_event
-- has no column that can hold it, so it gains an optional `detail`.
-- - A JSON object of at most 16 KiB (the acknowledgement is at most 4096 UTF-8
--   bytes, checked by the route), or NULL; every earlier row and every other
--   action keeps NULL.
-- - qw_app and qw_worker may insert it; 0002's statement trigger still refuses
--   every UPDATE, DELETE and TRUNCATE, so it is written once.
-- - Audit retention (owner decision 20: seven years) applies to it as to the row.

ALTER TABLE app.audit_event
    ADD COLUMN detail jsonb,
    ADD CONSTRAINT audit_event_detail_object CHECK (
        detail IS NULL
        OR (jsonb_typeof(detail) = 'object' AND octet_length(detail::text) <= 16384));

GRANT INSERT (detail) ON app.audit_event TO qw_app, qw_worker;
