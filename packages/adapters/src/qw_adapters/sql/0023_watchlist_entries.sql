-- 0023: watchlist entries (T024 increment 3; contract addWatchlistEntry,
-- updateWatchlistEntry, deleteWatchlistEntry). Extends 0022, which is not edited.
-- - Three entry actions. Each names the one instrument it adds, updates or removes
--   (instrument_id, covered by event_hash); the list-level actions name none.
-- - entries (a full snapshot per revision) holds at most 500 entries.
-- The adapter replays every entry action through the domain and re-encodes the
-- snapshot. The 0022 guard, lock, append-only triggers, RLS and grants still apply;
-- qw_app may also insert instrument_id.

ALTER TABLE app.watchlist_revision
    DROP CONSTRAINT watchlist_revision_action_check,
    ADD CONSTRAINT watchlist_revision_action_check CHECK (action IN (
        'create', 'update', 'delete', 'add_entry', 'update_entry', 'remove_entry')),
    ADD COLUMN instrument_id uuid,
    ADD CONSTRAINT watchlist_revision_instrument CHECK (
        (instrument_id IS NULL) = (action IN ('create', 'update', 'delete'))),
    ADD CONSTRAINT watchlist_revision_entry_limit CHECK (
        jsonb_array_length(entries) <= 500);

GRANT INSERT (instrument_id) ON app.watchlist_revision TO qw_app;
