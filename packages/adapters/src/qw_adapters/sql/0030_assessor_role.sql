-- 0030: owner decision 17 (2026-10-21). The runtime role qw_assessor (created
-- NOLOGIN in 0001; deploy-time logins are its members, as for qw_app and
-- qw_worker) alone writes the 0028 assessor tables and reads them, protected
-- columns included. qw_worker loses every privilege on them (it still appends
-- experiment_revision; the 0028 foreign keys are checked as the table owner).
-- qw_app keeps exactly its 14 non-protected receipt columns. qw_assessor also
-- reads experiment_revision (not protected; qw_app reads it too) so it can check a
-- transition before it stores a receipt. Grants only: no role is created.

REVOKE ALL ON app.assessor_protocol, app.assessor_candidate, app.holdout_access,
    app.evaluation_receipt FROM qw_worker;

GRANT EXECUTE ON FUNCTION app.current_tenant_id() TO qw_assessor;
GRANT SELECT ON app.assessor_protocol, app.assessor_candidate, app.holdout_access,
    app.evaluation_receipt, app.experiment_revision TO qw_assessor;
GRANT INSERT (tenant_id, experiment_id, protocol_hash, plan_id, baseline_version,
    assessor_actor_id, assessor_route_id, assessor_family_id, assessor_version,
    body, recorded_at, event_hash) ON app.assessor_protocol TO qw_assessor;
GRANT INSERT (tenant_id, experiment_id, candidate_id, depth, parent_id,
    content_hash, body, recorded_at, event_hash)
    ON app.assessor_candidate TO qw_assessor;
GRANT INSERT (tenant_id, plan_id, seq, access_id, trial_id, family, actor_id, body,
    recorded_at, event_hash) ON app.holdout_access TO qw_assessor;
GRANT INSERT (tenant_id, experiment_id, verdict_id, candidate_id, candidate_hash,
    protocol_hash, assessor_actor_id, assessor_route_id, assessor_family_id,
    assessor_version, decision, reasons, scores, trial_ids, failed_trial_ids,
    limitations, assessed_at, plan_id, scored_seq, ledger_entries, ledger_digest,
    recorded_at, event_hash) ON app.evaluation_receipt TO qw_assessor;
