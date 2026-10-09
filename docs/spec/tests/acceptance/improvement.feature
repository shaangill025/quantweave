Feature: Improvement acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT028 @R028
  Scenario: AT028 R028 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: All six improvement targets are in scope, including code and the optimizer itself.
    Then Each target has a candidate/evaluation/review/promotion/rollback path.

  @AT029 @R029
  Scenario: AT029 R029 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: Promotion uses predeclared scorecards with hard risk/privacy/evidence constraints, not recent returns alone.
    Then A candidate with higher simulated P&L but a hard violation is rejected.

  @AT030 @R030
  Scenario: AT030 R030 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: Autonomous authorized experiments, human-controlled promotion of investment-affecting or code changes.
    Then A model cannot self-approve its deployment or widen permissions.

  @AT032 @R032
  Scenario: AT032 R032 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: Separate experiment resource budgets; current monitoring and verification take priority.
    Then Experiment exhaustion pauses experiments while essential services remain available.

  @AT033 @R033
  Scenario: AT033 R033 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: All six improvement areas, code-level change and recursive optimizer change must work at first release.
    Then Full release cannot pass by labelling code/recursive paths future work or disabling them.

  @AT037 @R037
  Scenario: AT037 R037 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: Demonstrate V0 optimizer proposing V1, independent evaluation, human promotion, then V1 running next cycle.
    Then Lineage and actual version receipts prove changed implementation was used, including rejected controls.

  @AT039 @R039
  Scenario: AT039 R039 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: User learning is tenant-scoped; shared executable/optimizer changes are installation/release scoped.
    Then Hosted user cannot deploy a private executable fork into shared production.

  @AT076 @R076
  Scenario: AT076 R076 acceptance
    Given A versioned test environment and authorized fixture for improvement
    When the implemented behavior is exercised: gVisor initial Linux sandbox qualification target; full recursive scope; reviewed signed promotion and compatible rollback.
    Then Unsupported sandbox cannot qualify full release by dropping mandatory improvement paths.
