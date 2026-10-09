Feature: Portfolio acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT012 @R012
  Scenario: AT012 R012 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Stale or unreconciled holdings/cash block affected portfolio-specific sizing.
    Then A stale account yields research or hypothetical output, not confirmed buying-power sizing.

  @AT042 @R042
  Scenario: AT042 R042 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Partial portfolios allowed with scoped conclusions; outside-coverage assets retained with gaps.
    Then Unknown outside exposure is not zero; full-financial-situation claims are withheld.

  @AT043 @R043
  Scenario: AT043 R043 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Multiple accounts/currencies with account-specific spendability and consolidated risk.
    Then Cash in another account/currency cannot fund a proposal without an explicit prerequisite.

  @AT046 @R046
  Scenario: AT046 R046 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Tax-relevant records and warnings, not a comprehensive tax engine or automated tax-loss harvesting.
    Then P&L/cost basis provenance is shown without presenting it as definitive tax liability.

  @AT058 @R058
  Scenario: AT058 R058 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Underlying accounts and data sources are separate; reconcile overlapping views without duplication.
    Then A broker position mirrored in Yahoo totals once; two real accounts remain distinct.

  @AT071 @R071
  Scenario: AT071 R071 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Reconciled decimal journal with explicit effective/publication/receipt/decision times; one calculation contract.
    Then Source corrections update current reconstruction without rewriting historical knowledge.

  @AT081 @R081
  Scenario: AT081 R081 acceptance
    Given A versioned test environment and authorized fixture for portfolio
    When the implemented behavior is exercised: Immutable source observations map into balanced postings or quarantined exceptions; provisional views never double-post.
    Then Journal balance, idempotency, correction reversal and lot/cash reconciliation fixtures pass.
