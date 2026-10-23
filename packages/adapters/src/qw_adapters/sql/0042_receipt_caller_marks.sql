-- 0042: owner decisions 43 and 66 (2026-10-21 and 2026-10-23).
-- Every evaluation receipt is marked `ledger_caller_supplied` and
-- `book_caller_supplied` (decision 43's "budget caller-supplied"; a column named
-- for the budget would be money-named for schema_guard, and this one is no
-- amount): the trial ledger and the budget book it was assessed
-- on are the caller's (decision 43), until the T032 and T037 stores exist and
-- replace the marks with their own values. Like decision 33's verdict_source on a
-- signed release (0032): hashed by the store (assessor_store), set when the
-- verdict is recorded, and a NOT VALID CHECK binds every new row to the one value
-- true today; rows recorded before this migration keep NULL and their hashes.
-- Reviewers see the marks wherever they see the verdict: qw_app reads them with
-- the receipt's other non-protected columns. qw_assessor writes them; no other
-- grant changes.

ALTER TABLE app.evaluation_receipt
    ADD COLUMN ledger_caller_supplied boolean,
    ADD COLUMN book_caller_supplied boolean,
    ADD CONSTRAINT evaluation_receipt_caller_marks CHECK (
        ledger_caller_supplied IS TRUE AND book_caller_supplied IS TRUE) NOT VALID;

GRANT INSERT (ledger_caller_supplied, book_caller_supplied)
    ON app.evaluation_receipt TO qw_assessor;
GRANT SELECT (ledger_caller_supplied, book_caller_supplied)
    ON app.evaluation_receipt TO qw_app;
