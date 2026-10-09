Feature: Options acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT017 @R017
  Scenario: AT017 R017 acceptance
    Given A versioned test environment and authorized fixture for options
    When the implemented behavior is exercised: Opt-in first-release options include long calls/puts, covered calls, cash-secured puts and defined-risk verticals.
    Then All permitted structures have scenarios and checks; out-of-scope imported contracts remain represented.

  @AT089 @R089
  Scenario: AT089 R089 acceptance
    Given A versioned test environment and authorized fixture for options
    When the implemented behavior is exercised: Option contract deliverables, adjusted multipliers, exercise style and settlement are explicit, not defaulted blindly.
    Then Nonstandard/unknown contracts remain represented but unsupported sizing/payoffs are blocked.
