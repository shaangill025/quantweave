-- Assessor-run input reads (T027, T021; owner decisions 70 and 78, 2026-10-23).
-- Decision 70: qw_assessor gets read-only grants on what an assessor run must read.
-- Decision 78: it reads strategy manifests but not adoption records (the candidate
-- manifest is re-derived from the base manifest and checked against its bound
-- hash, so no table is added). The research rights registry is read through
-- qw_adapters.rights_store.load, which reads exactly the five 0013 tables below.
-- - strategy_manifest and the four global rights tables keep their open read
--   policies (0013, 0015): installation facts, the same for every tenant.
-- - rights_entitlement keeps its forced tenant isolation policy, which applies to
--   every role, so qw_assessor sees only the transaction tenant's entitlements.
--   qw_assessor already holds EXECUTE on app.current_tenant_id() (0030).
-- - strategy_adoption: no privilege at all (decision 78).
-- - SELECT only: inserts stay with the schema owner (0013, 0015); the append-only
--   statement triggers are unchanged.
-- Grants only: no role, table, policy or function is created.

GRANT SELECT ON app.strategy_manifest TO qw_assessor;
GRANT SELECT ON app.rights_provider, app.rights_feed, app.rights_feed_revision,
    app.rights_feed_transition, app.rights_entitlement TO qw_assessor;
