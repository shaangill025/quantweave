Feature: Analytics acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT064 @R064
  Scenario: AT064 R064 acceptance
    Given A versioned test environment and authorized fixture for analytics
    When the implemented behavior is exercised: Versioned goal-appropriate/cash-flow-matched benchmarks and separate actual/simulated/recommendation scorecards.
    Then No fabricated historical cash flows; TWR/MWR calculated only with sufficient inputs.

  @AT090 @R090
  Scenario: AT090 R090 acceptance
    Given A versioned test environment and authorized fixture for analytics
    When the implemented behavior is exercised: No-unique-root MWR, nonpositive equity and missing valuation intervals return explicit unavailable status.
    Then Return fixtures identify ambiguity rather than invent a percentage.
