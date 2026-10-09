Feature: Ux acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT047 @R047
  Scenario: AT047 R047 acceptance
    Given A versioned test environment and authorized fixture for ux
    When the implemented behavior is exercised: Responsive web, in-app feed, configurable email/browser push; no native mobile/SMS launch requirement.
    Then Expiry-aware notifications work and self-hosting needs no proprietary notification server.

  @AT056 @R056
  Scenario: AT056 R056 acceptance
    Given A versioned test environment and authorized fixture for ux
    When the implemented behavior is exercised: Dashboard-led portfolio/decision/research/governance workspaces; chat uses same verified state.
    Then Chat cannot emit an actionable recommendation outside the governed proposal pipeline.

  @AT093 @R093
  Scenario: AT093 R093 acceptance
    Given A versioned test environment and authorized fixture for ux
    When the implemented behavior is exercised: Keyboard-accessible financial tables and text states accompany colors; screen freshness is continuously visible.
    Then Keyboard-only and screen-reader journeys cover policy, reconciliation and proposal expiry.
