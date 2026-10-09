Feature: Proposals acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT020 @R020
  Scenario: AT020 R020 acceptance
    Given A versioned test environment and authorized fixture for proposals
    When the implemented behavior is exercised: Action proposals expire and have invalidation conditions plus preserved version history.
    Then Late notifications resolve to current state; corrected decisions do not overwrite original results.
