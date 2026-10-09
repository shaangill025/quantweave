Feature: Integrations acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT048 @R048
  Scenario: AT048 R048 acceptance
    Given A versioned test environment and authorized fixture for integrations
    When the implemented behavior is exercised: Small named set of tested read-only broker sync routes; CSV/manual fallback.
    Then Each connector passes fields, pagination, reconnect and reconciliation tests, not only login.

  @AT053 @R053
  Scenario: AT053 R053 acceptance
    Given A versioned test environment and authorized fixture for integrations
    When the implemented behavior is exercised: CIBC import, Questrade/IBKR read-only sync, plus Wealthsimple sync and Yahoo CSV interoperability.
    Then Named commitments each have deployment-specific qualification evidence; no silent downgrade to generic CSV.

  @AT057 @R057
  Scenario: AT057 R057 acceptance
    Given A versioned test environment and authorized fixture for integrations
    When the implemented behavior is exercised: Yahoo tested CSV portfolio/watchlist import and compatible export; no auto-sync/data-adapter launch promise.
    Then Round-trip differences are shown and Yahoo watchlists never imply owned shares.

  @AT073 @R073
  Scenario: AT073 R073 acceptance
    Given A versioned test environment and authorized fixture for integrations
    When the implemented behavior is exercised: Capability/rights/degraded-state registry; provider/entitlement changes require reassessment.
    Then Connected status does not imply qualified; tenant feed sharing requires rights and consent.
