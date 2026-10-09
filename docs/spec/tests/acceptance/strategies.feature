Feature: Strategies acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT008 @R008
  Scenario: AT008 R008 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: Intraday swing discovery plus optional same-day opportunities; no sub-minute scalping requirement.
    Then Qualified regular-session strategy can produce timely proposals under its data/model configuration.

  @AT009 @R009
  Scenario: AT009 R009 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: Frequent evaluation means timely qualifying changes, not a quota of trades.
    Then A no-opportunity session records zero proposals and explicit rejection reasons without manufactured trades.

  @AT050 @R050
  Scenario: AT050 R050 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: Initial actionable intraday proposals are regular-session only; research may run outside session.
    Then Premarket event can create conditional research but must be refreshed before activation.

  @AT059 @R059
  Scenario: AT059 R059 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: All six versioned stock/ETF strategy families required; qualification status separate from catalogue inclusion.
    Then Each family has documented logic/evaluation; failed variants remain research-only without edge claims.

  @AT061 @R061
  Scenario: AT061 R061 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: Bounded versioned strategy configuration, not unrestricted hosted arbitrary-code or visual strategy builder.
    Then Configuration changes enter relevant validation/adoption; developer code extension remains controlled.

  @AT062 @R062
  Scenario: AT062 R062 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: Free baseline supports portfolio-specific rebalancing plus a qualified non-intraday long-only strategy.
    Then End-to-end free positive and abstention journeys pass, not only dashboards/alerts.

  @AT072 @R072
  Scenario: AT072 R072 acceptance
    Given A versioned test environment and authorized fixture for strategies
    When the implemented behavior is exercised: Interpretable six-family starting implementations; 6-month/200-day momentum and 15-minute ORB are unqualified research defaults.
    Then Parameter choices are labelled candidates and cannot auto-enable live strategies.
