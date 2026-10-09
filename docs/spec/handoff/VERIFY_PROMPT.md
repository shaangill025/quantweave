# Verification prompt

Verify only the assigned task against its acceptance criteria using commands actually available and authorized in the repository. Capture exact command, environment, start/end, exit status, evidence files and unresolved failures. Confirm negative controls fail for the expected reason. Distinguish offline unit/mocked behavior from real integration and commercial approval.

For any skipped check state why it was skipped and which release gate remains open. Never turn 'not run' into 'passed'. Do not loosen thresholds or data requirements to obtain a pass. The specification validator is not an application test. Complete handoff/TASK_RECEIPT_TEMPLATE.md without inventing outputs.
