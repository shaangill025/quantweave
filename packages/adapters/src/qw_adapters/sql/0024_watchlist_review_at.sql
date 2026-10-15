-- 0024: a settable watchlist review time (T024 increment 4; project contract 0.5.0,
-- owner decision 2026-10-15). Extends 0022 and 0023, which are not edited.
-- - review_at_set records whether an update_entry request set the entry's
--   review_at. The adapter's replay lets such a row change review_at; any other
--   update_entry row must keep it. needs is always kept.
-- - Existing rows read false: the default fills them without an UPDATE, so the
--   append-only triggers are not involved and their hashes are unchanged (the flag
--   is hashed only when true).

ALTER TABLE app.watchlist_revision
    ADD COLUMN review_at_set boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT watchlist_revision_review_at_set CHECK (
        NOT review_at_set OR action = 'update_entry');

GRANT INSERT (review_at_set) ON app.watchlist_revision TO qw_app;
