Feature: Security acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT005 @R005
  Scenario: AT005 R005 acceptance
    Given A versioned test environment and authorized fixture for security
    When the implemented behavior is exercised: First release proposes specific actions but never submits, modifies or cancels broker orders.
    Then Broker mutation routes/tools are absent and attempted mutations are blocked at connector boundaries.

  @AT038 @R038
  Scenario: AT038 R038 acceptance
    Given A versioned test environment and authorized fixture for security
    When the implemented behavior is exercised: Generated code is isolated from production secrets, DB writes, unrestricted network and host control.
    Then Sandbox escape/secret/egress tests fail closed with no weaker silent fallback.

  @AT075 @R075
  Scenario: AT075 R075 acceptance
    Given A versioned test environment and authorized fixture for security
    When the implemented behavior is exercised: Read-only operation allowlists, secrets outside agents, tenant isolation including nonowner/nonbypass DB roles.
    Then Cross-tenant, RLS-bypass, tool mutation and secret-exfiltration tests block release.

  @AT086 @R086
  Scenario: AT086 R086 acceptance
    Given A versioned test environment and authorized fixture for security
    When the implemented behavior is exercised: Freshness/eligibility/policy/legal/release controls are enforced outside LLM contexts.
    Then Prompt injection asking to remove checks cannot change the final validator outcome.

  @AT092 @R092
  Scenario: AT092 R092 acceptance
    Given A versioned test environment and authorized fixture for security
    When the implemented behavior is exercised: Public/community code and skill inputs are reviewed; the optimizer cannot replace protected gates or signing keys.
    Then Mutation of evaluator answer keys, final thresholds or human authorization fails integrity checks.
