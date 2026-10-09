Feature: Cross-domain safety and data integrity
  Scenario: Same Wealthsimple account mirrored in Yahoo
    Given SYN-WS has 50 shares from its brokerage and the same 50 in a mapped Yahoo view
    And SYN-OTHER has 50 shares in a distinct brokerage account
    When all three source rows are reconciled
    Then consolidated holdings are 100 shares and source lineage has three observations

  Scenario: Concurrent cash-consuming publication
    Given account revision 7 has USD 5000 free and two USD 4000 proposals
    When two workers concurrently attempt to publish both as a jointly feasible set
    Then at most one USD 4000 planning commitment succeeds
    And the other is a typed revision or capital conflict rather than an unrecorded retry

  Scenario: Fresh price cannot refresh delayed account source
    Given a price received one second ago and account information 360 seconds old
    And the adopted account freshness bound is 300 seconds
    When an intraday evaluator returns a compelling buy recommendation
    Then affected sizing is blocked with ACCOUNT_STALE
    And the price timestamp does not replace the account effective timestamp

  Scenario: Evaluator introduces a new material factual claim
    Given an evaluator replaces a thesis using a previously unchecked contract-win claim
    When the revised proposal reaches final checks
    Then it cannot become active until the new claim has supporting evidence and required review
    And an unmodified old review hash cannot authorize the new proposal hash

  Scenario: Fast enough review but expired setup
    Given a setup ceased to be valid at trigger plus 20 seconds
    When review completes at trigger plus 45 seconds before the 60 second budget
    Then the proposal is not published as actionable
    And the expired-before-review event is counted in operational reporting

  Scenario: Unknown adjusted options contract
    Given a held adjusted option has unknown deliverables and multiplier
    When the user disables options suggestions
    Then its exposure and data gap remain visible
    And no live sizing assumes a 100-share contract

  Scenario: Claimed recursive improvement without actual V1 execution
    Given a candidate optimizer V1 has a written self-assessment
    But no independent assessment or authorized promotion receipt exists
    When the release checker evaluates full recursive capability
    Then the release gate fails
    And a simulated statement that V1 ran does not satisfy the required next-cycle receipt

  Scenario: Protected assessor cannot be improved by the candidate under assessment
    Given a code experiment attempts to replace its final test definitions
    When the sandbox permission layer rejects that write
    Then the attempt is recorded and cannot improve the candidate score
    And the production assessor and promotion keys remain unchanged

  Scenario: Budget reservation prevents concurrent overspend
    Given USD 75 spent and USD 10 reserved against a USD 100 monthly cap
    When another job requests a USD 20 upper-bound reservation
    Then no chargeable provider request is dispatched
    And experiment workers cannot use a different provider account as an unapproved fallback

  Scenario: Delete then restore backup
    Given a user's deletion tombstone postdates the most recent application backup
    When recovery restores the old backup
    Then deletion suppression runs before affected records become visible
    And unavailable source evidence is not shown as fully reproducible
