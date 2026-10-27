-- 0048: owner decisions 92 and 93 (T050; 2026-10-26 and 2026-10-27). Installation
-- jobs ride on the installing operator's tenant queue (92); for self-hosted, that
-- tenant is the installation's bootstrap tenant (0004 app.installation_bootstrap,
-- the first tenant `qw-bootstrap` creates), and an `assessment.optimizer` job from
-- any other tenant is refused before any registry read (93).
-- The check runs in the qw-assessor process on its qw_assessor login, which only
-- that process holds (decision 40). qw_worker is not granted: every ordinary
-- worker holds it. The read is one column of the single row; no write, no other
-- column. RLS is unchanged: 0004's `installation_scope` policy (USING (true), for
-- every role) already admits the row, and the column grant is what limits it.
-- Hosted editions have no bootstrap row; there the check fails closed until they
-- get their own binding.

GRANT SELECT (tenant_id) ON app.installation_bootstrap TO qw_assessor;
