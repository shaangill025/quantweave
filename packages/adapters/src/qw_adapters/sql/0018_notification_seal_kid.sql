-- 0018: notification seal key ids (T056 increment 3). Every sealed notification row
-- names the kid of the notification keyring key that sealed it
-- (qw_adapters.notification_store.Keyring, separate from the API secret_key, so
-- rotating secret_key no longer invalidates stored notifications; old kids stay
-- verify-only). Rows written under 0017 have no kid and fail closed (`integrity`;
-- the feed serves none of their content and names them in coverage): they were
-- sealed and identified under a secret_key derivation that is no longer accepted,
-- and nothing was deployed. New rows must carry a kid (NOT VALID: the NOT NULL
-- check applies to new rows only, so legacy rows stay as recorded).

ALTER TABLE app.notification
    ADD COLUMN seal_kid text CHECK (seal_kid ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    ADD CONSTRAINT notification_seal_kid_set CHECK (seal_kid IS NOT NULL) NOT VALID;
ALTER TABLE app.notification_delivery
    ADD COLUMN seal_kid text CHECK (seal_kid ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    ADD CONSTRAINT notification_delivery_seal_kid_set
        CHECK (seal_kid IS NOT NULL) NOT VALID;
ALTER TABLE app.notification_delivery_event
    ADD COLUMN seal_kid text CHECK (seal_kid ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    ADD CONSTRAINT notification_delivery_event_seal_kid_set
        CHECK (seal_kid IS NOT NULL) NOT VALID;
ALTER TABLE app.notification_seen
    ADD COLUMN seal_kid text CHECK (seal_kid ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'),
    ADD CONSTRAINT notification_seen_seal_kid_set
        CHECK (seal_kid IS NOT NULL) NOT VALID;

GRANT INSERT (seal_kid) ON app.notification TO qw_worker;
