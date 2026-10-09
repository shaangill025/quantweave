Feature: Governance acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT034 @R034
  Scenario: AT034 R034 acceptance
    Given A versioned test environment and authorized fixture for governance
    When the implemented behavior is exercised: Material changes to enabled investment strategies require user adoption after release approval.
    Then Maintainer approval does not silently change an existing user's strategy semantics.

  @AT079 @R079
  Scenario: AT079 R079 acceptance
    Given A versioned test environment and authorized fixture for governance
    When the implemented behavior is exercised: Separate design choices from factual provider/legal/security/performance/economic gates.
    Then No completion claim without versioned evidence and no silent scope reduction when a gate fails.

  @AT094 @R094
  Scenario: AT094 R094 acceptance
    Given A versioned test environment and authorized fixture for governance
    When the implemented behavior is exercised: All external-provider feature claims are dated and distinguish docs, permission, auth and workload evidence.
    Then A docs-only record cannot become commercial-qualified without required receipts.

  @AT099 @R099
  Scenario: AT099 R099 acceptance
    Given A versioned test environment and authorized fixture for governance
    When the implemented behavior is exercised: Runtime approvals are typed by maintainer release, operator install and user strategy adoption; planning delegation is not runtime consent.
    Then A conversational yes cannot satisfy a production promotion or investment-policy signature.
