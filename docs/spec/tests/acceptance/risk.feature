Feature: Risk acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT010 @R010
  Scenario: AT010 R010 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Separate investing and trading allocations with portfolio-wide limits.
    Then A trading proposal cannot spend a long-term reserve and combined exposure is checked.

  @AT044 @R044
  Scenario: AT044 R044 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Stock/ETF proposals cash-funded long-only, nonleveraged/noninverse; approved options remain.
    Then Short/leveraged discovery cannot become a new action; existing such positions still contribute known exposure.

  @AT049 @R049
  Scenario: AT049 R049 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Proposals form jointly feasible sets with explicit alternatives and internal capital commitments.
    Then Two USD4000 proposals cannot jointly consume USD5000; unexecuted sales do not create cash.

  @AT060 @R060
  Scenario: AT060 R060 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Conservative liquidity/spread/data/status-screened universe; optional speculative research cannot bypass gates.
    Then OTC/penny/failed checks excluded from default actionable discovery while existing exposure remains tracked.

  @AT068 @R068
  Scenario: AT068 R068 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Explicit approved numerical account/allocation/concentration/planned-loss/scenario limits for sizing.
    Then Denominators and currencies displayed; missing mandatory limits block sizing; stops are not loss guarantees.

  @AT069 @R069
  Scenario: AT069 R069 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Scoped loss/drawdown triggers pause new risk; monitoring and checked risk reduction continue; controlled resumption.
    Then Deposits cannot erase breaches; material resume needs reconciled state and explicit acknowledgement.

  @AT082 @R082
  Scenario: AT082 R082 acceptance
    Given A versioned test environment and authorized fixture for risk
    When the implemented behavior is exercised: Final publication atomically binds policy, account and proposal-set revisions to a checked cash commitment.
    Then A competing account update causes re-evaluation, never time-of-check/time-of-use overspend.
