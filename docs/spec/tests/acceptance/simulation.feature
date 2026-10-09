Feature: Simulation acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT063 @R063
  Scenario: AT063 R063 acceptance
    Given A versioned test environment and authorized fixture for simulation
    When the implemented behavior is exercised: User-facing application-managed virtual portfolios and optional approved-policy shadow tracking.
    Then Simulation never changes real cash and clearly labels fills, costs, assumptions and unsupported cases.

  @AT098 @R098
  Scenario: AT098 R098 acceptance
    Given A versioned test environment and authorized fixture for simulation
    When the implemented behavior is exercised: A simulated fill must occur after modeled submission and under a declared available-data convention.
    Then Same-bar ambiguity resolves conservatively or as indeterminate, never automatically best-case.
