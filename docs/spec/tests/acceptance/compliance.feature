Feature: Compliance acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT007 @R007
  Scenario: AT007 R007 acceptance
    Given A versioned test environment and authorized fixture for compliance
    When the implemented behavior is exercised: Initial commercial audience includes Canadian and US residents.
    Then Jurisdiction inventory and counsel-approved capability matrix cover intended launch customers in both countries.

  @AT021 @R021
  Scenario: AT021 R021 acceptance
    Given A versioned test environment and authorized fixture for compliance
    When the implemented behavior is exercised: Commercial legal and provider-rights clearance are release gates.
    Then Documentation access and disclaimers do not mark rights/regulatory gates passed.
