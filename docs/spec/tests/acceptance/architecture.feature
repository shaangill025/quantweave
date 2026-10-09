Feature: Architecture acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT070 @R070
  Scenario: AT070 R070 acceptance
    Given A versioned test environment and authorized fixture for architecture
    When the implemented behavior is exercised: Typed Python core, TypeScript/React, PostgreSQL, analytical file/object storage and durable jobs; three authority paths.
    Then Live, experiment and release operations share contracts but cannot share unrestricted authority.

  @AT080 @R080
  Scenario: AT080 R080 acceptance
    Given A versioned test environment and authorized fixture for architecture
    When the implemented behavior is exercised: All API monetary amounts/quantities use decimal strings; times use UTC with exchange calendar identity.
    Then Schema rejects binary numeric money and timestamps without offsets.
