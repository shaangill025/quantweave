Feature: Platform acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT006 @R006
  Scenario: AT006 R006 acceptance
    Given A versioned test environment and authorized fixture for platform
    When the implemented behavior is exercised: Both hosted and complete self-hosted editions are release requirements.
    Then Both deployment profiles pass core journeys and isolation checks.

  @AT013 @R013
  Scenario: AT013 R013 acceptance
    Given A versioned test environment and authorized fixture for platform
    When the implemented behavior is exercised: Independent open-source core and complete self-hosted application; no mandatory vendor-operated service.
    Then Disconnect vendor services and exercise permitted core workflows with user-selected dependencies.

  @AT022 @R022
  Scenario: AT022 R022 acceptance
    Given A versioned test environment and authorized fixture for platform
    When the implemented behavior is exercised: Apache-2.0 is the selected application license, superseding AGPL proposals.
    Then License and dependency inventory reflect Apache choice without bundling incompatible material by assumption.

  @AT036 @R036
  Scenario: AT036 R036 acceptance
    Given A versioned test environment and authorized fixture for platform
    When the implemented behavior is exercised: Single-machine container self-hosting without required Kubernetes/GPU/vendor service.
    Then Clean supported installation, backup restore and core workflows complete on qualified hardware.
