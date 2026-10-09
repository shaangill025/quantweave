# 6. Market data, reference sources and integration qualification

## Three connector classes

Account connectors describe holdings/cash/activity. Interchange adapters import/export user files. Market/research adapters supply prices, filings, calendars and related evidence. A successful implementation in one class does not imply permission or functionality in another. Each provider/deployment record tracks documentation, contractual permission, authentication, field coverage and workload qualification separately.

The capability registry includes provider/plan/feed, allowed instruments, observations/history, quotas, sessions, latency mode, retention, display/export/redistribution/model-processing rights, commercial usage, allowed auth route, review date and qualifying receipts. Default statuses are unqualified until evidence exists. Direct user credentials never authorize centrally pooling an individual feed across tenants.

## Selected integration commitments

| Source | First-release role | Qualification boundary |
|---|---|---|
| Alpaca | Free IEX monitored set; qualified delayed/EOD historical inputs; optional consolidated/OPRA configuration | Actual entitlement, provider use rights and strategy-specific fitness [SRC-01, SRC-02] |
| SEC EDGAR | Primary US company filings and normalized reported facts | Publication-aware units/restatements, permitted retention and fair access [SRC-10] |
| Bank of Canada Valet | Daily CAD/USD reference FX | Not executable pricing; time/series/term checks [SRC-11] |
| CIBC Investor's Edge | Tested portfolio import/reconciliation | Authorized real export samples; no automatic sync claimed |
| Questrade | Read-only sync, direct or qualified aggregator route | Separate personal/commercial route and field tests [SRC-06] |
| Interactive Brokers | Read-only sync via qualified route | Vendor approval/auth/session/field qualification [SRC-07] |
| Wealthsimple | Read-only sync via qualified route; CSV fallback | SnapTrade candidate plus actual tests; fallback not a silent replacement [SRC-04, SRC-08] |
| Yahoo Finance | Portfolio/watchlist CSV import and compatible export | No automatic account sync or Yahoo data license; exact formats remain sample-qualified [SRC-09] |

Additional paid sources may implement the same contracts after rights and fitness review. They are not necessary to make the free baseline useful. No scraping bypass, unofficial token capture or undocumented redistribution route is an accepted fallback.

## Free monitoring workload

Maintain separate tracked universe, scheduled-discovery universe and active streamed set. Prioritize owned/risk-sensitive positions and user-prioritized candidates with explicit slot allocation. A 250-instrument account does not imply 250 live subscriptions. When the provider allows 30 stock streams, expose the remaining scheduled coverage and avoid secret rotation that masquerades as continuity. Batch historical requests within quotas and use permitted caching. [SRC-01]

Qualify daily consolidated history with explicit `feed=sip` and permitted cutoff rather than relying on subscription-selected defaults. Test coverage gaps, corporate actions and required history. IEX volume is not consolidated volume. A strategy requiring unavailable data stays disabled for that configuration, not silently weakened. [SRC-02]

## Freshness is multidimensional

For every proposal evaluate market-event age, receipt delay, portfolio observation age, unresolved account changes, fundamental publication age, calendar validity and FX reference age. Target processing times in Q67 start from recorded trigger receipt but do not make old observations fresh.

SnapTrade documents trade detection for Wealthsimple and Questrade at five-minute-or-longer intervals. Do not claim second-by-second confirmed buying power because quotes refresh faster. [SRC-05] Broker account freshness profiles are independently qualified. A fresh user attestation of no intervening trades is recorded as an attestation and never resets the broker-observed timestamp or upgrades it to broker-confirmed. When required current account state is unavailable, withhold sizing while continuing general research.

## Normalization and continuity

Market observations carry feed-spec hash, source event ID, security/listing ID, raw/adjusted mode, session, eligible conditions, event/publication/receipt time, sequence/gap flags and corrections. Build bars only with specified trade/quote eligibility and left/right interval conventions. Do not backfill the current opening range with unavailable future data or a different feed without reinitialization.

On feed/entitlement change: check compatibility; revalidate rights and expected timing; recover state only from permissible data; invalidate/reassess affected candidates. A reconstructed historical opportunity is retrospective. Suspend affected action flows during incompatible gaps while preserving unrelated services.

## Connector conformance

Test authentication/reauthentication, consent revocation, API limits, pagination, corrected and duplicate events, account selection, currencies, source data timestamps, option identifiers, failed/partial sync, permissions and provider outages. A connector returns observations and diagnostics, not direct journal postings. Secrets remain in an isolated vault/connector runtime and never in model context.

Broker mutations are denied by route/method operation allowlists even where a broad credential could technically perform them. A GET returning orders is account context; it is not authority to create or cancel orders. Distinguish reported open orders from planning commitments so the same cash is not encumbered twice.

## Rights-aware evidence and exports

Content can be stored, indexed, displayed, sent to a model, or exported only under its qualified use profile. Deny by default when rights are unknown. When an evidence snapshot cannot be retained, store permitted metadata and make reproducibility limits explicit. Migration includes a rights-exclusion report. Provider policy changes revoke dependent qualification rather than retrospectively pretending permission was always known.
