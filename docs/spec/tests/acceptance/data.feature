Feature: Data acceptance contracts
  These specifications require step implementations in the application test suite.
  No scenario in this file has been executed against an application.

  @AT003 @R003
  Scenario: AT003 R003 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: A useful no-paid-market-data baseline must not require paid AI; hosting and optional providers are separate.
    Then Rules-only onboarding and a qualified decision workflow complete with zero paid data and zero AI calls.

  @AT004 @R004
  Scenario: AT004 R004 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: Mandatory discovery is US-listed stocks and ETFs with currency-aware accounts.
    Then US instrument identity and CAD/USD accounting work; outside-scope holdings remain visible.

  @AT011 @R011
  Scenario: AT011 R011 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: Free mode may require a user-connected free provider account/key.
    Then Setup discloses provider account, limits and rights; key connection alone does not mark capabilities qualified.

  @AT014 @R014
  Scenario: AT014 R014 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: Prioritized live monitoring plus broader scheduled discovery; tracked and streamed universes differ.
    Then Exceeding live-symbol quota produces visible prioritization, not fictitious continuous coverage.

  @AT066 @R066
  Scenario: AT066 R066 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: Versioned feed specifications and qualified continuity/reinitialization for provider changes.
    Then A mid-session IEX/SIP change cannot continue the old range silently; recovered signals are retrospective.

  @AT083 @R083
  Scenario: AT083 R083 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: Separate price, fundamental, calendar, broker and FX freshness policies; record event age and ingestion delay.
    Then Fast processing of five-minute-old broker state never labels it real-time confirmed.

  @AT091 @R091
  Scenario: AT091 R091 acceptance
    Given A versioned test environment and authorized fixture for data
    When the implemented behavior is exercised: Exchange sessions include holidays, DST, early closes, auctions and halts in versioned calendars.
    Then ORB and expiry deadlines are session-derived rather than fixed UTC strings.
