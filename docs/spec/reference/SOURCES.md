# Source register

Checked 8 October 2026. Source facts are distinguished from selected design requirements. No provider credentials were supplied; no authenticated integration was tested. The source summaries are deliberately bounded; linked documentation must be rechecked at the relevant release gate.

## SRC-01 — Alpaca Market Data plans

https://docs.alpaca.markets/us/docs/about-market-data-api

**Access:** Official documentation inspected. **Supports:** Individual Basic lists IEX-only stocks, 30 stock streams, 200 historical requests/minute, indicative options. Individual and business arrangements differ.

**Boundary:** Provider/runtime/rights testing still required.

## SRC-02 — Alpaca feed semantics

https://docs.alpaca.markets/us/docs/market-data-faq

**Access:** Official documentation inspected. **Supports:** Historical feed selection is explicit; free SIP history requires end at least 15 minutes old. Subscription-default feed selection can change.

**Boundary:** No assumption of consolidated live quotes in the free mode.

## SRC-03 — Alpaca paper environment

https://docs.alpaca.markets/us/docs/paper-trading

**Access:** Official documentation inspected. **Supports:** Paper simulation omits important real execution effects. Paper-only access is a candidate for free onboarding.

**Boundary:** Our simulator must declare its own economics; paper results are not proof of real performance.

## SRC-04 — SnapTrade broker access

https://docs.snaptrade.com/docs/broker-access-guide

**Access:** Official documentation inspected. **Supports:** Broker guide lists supported integration routes, with production access requirements.

**Boundary:** Test each broker, account type, auth method, deployment and field; names on a list are not acceptance evidence.

## SRC-05 — SnapTrade trade detection

https://docs.snaptrade.com/docs/trade-detection

**Access:** Official documentation inspected. **Supports:** Wealthsimple and Questrade trade detection is documented at five-minute-or-longer intervals.

**Boundary:** Account-state freshness must be separate from quote and processing latency.

## SRC-06 — Questrade API

https://www.questrade.com/api

**Access:** Official documentation inspected. **Supports:** Personal users can access account/market information; API trade execution is limited to partner developers.

**Boundary:** Read-only capabilities only; company integration permission is a separate gate.

## SRC-07 — IBKR Web API

https://www.interactivebrokers.com/campus/ibkr-api-page/webapi-doc/

**Access:** Official documentation inspected. **Supports:** Read-only portfolio subset exists. Third-party vendor integration requires approval and a distinct authentication process.

**Boundary:** No unattended commercial or local auth route assumed from a personal login.

## SRC-08 — Wealthsimple activity export

https://help.wealthsimple.com/hc/en-ca/articles/39449252453019-View-your-account-activity-and-running-balance

**Access:** Official documentation inspected. **Supports:** Activity CSV download is documented.

**Boundary:** Real anonymized samples and corporate-action reconciliation are not yet tested.

## SRC-09 — Yahoo portfolio CSV

https://help.yahoo.com/kb/finance-for-web/download-portfolio-data-yahoo-finance-sln15034.html

**Access:** Official search excerpt only; direct open failed. **Supports:** Yahoo help describes portfolio/list CSV transfer.

**Boundary:** Exact current columns, round-trip loss and transaction completeness remain sample-based qualification gates. Not a Yahoo market data license.

## SRC-10 — SEC EDGAR APIs

https://www.sec.gov/search-filings/edgar-application-programming-interfaces

**Access:** Official documentation inspected. **Supports:** SEC provides submissions and XBRL APIs.

**Boundary:** Respect fair-access rules; publication dates, units and restatements require normalization. No analyst-consensus promise.

## SRC-11 — Bank of Canada Valet

https://www.bankofcanada.ca/valet-api-how-to/

**Access:** Official documentation inspected. **Supports:** Valet has no usage charge, registration or access-key requirement; daily data can be cached.

**Boundary:** Reference FX, not an executable conversion quote; respect terms and data timing.

## SRC-12 — ChatGPT plan in other apps

https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites

**Access:** Official documentation inspected. **Supports:** Plan-funded requests are available in participating apps/tools subject to eligibility and plan rules.

**Boundary:** App participation, deployment and permissible financial use must be qualified; no universal subscription API.

## SRC-13 — Claude Code authentication rules

https://code.claude.com/docs/en/legal-and-compliance

**Access:** Official documentation inspected. **Supports:** Native unmodified Claude Code authentication is distinguished from prohibited collection/intermediation of Claude.ai credentials by third parties.

**Boundary:** Use API keys or approved native flow; no session-token proxy. Hosted feasibility remains open.

## SRC-14 — xAI API authentication

https://docs.x.ai/developers/rest-api-reference/management/auth

**Access:** Official documentation inspected. **Supports:** API keys authenticate inference requests as bearer credentials.

**Boundary:** API adapter required; consumer-subscription access is not promised.

## SRC-15 — PostgreSQL row security

https://www.postgresql.org/docs/current/ddl-rowsecurity.html

**Access:** Official documentation inspected. **Supports:** Superusers, BYPASSRLS roles and ordinarily table owners can bypass row security.

**Boundary:** Use nonowner/nonbypass service roles and explicit isolation tests; RLS alone is insufficient.

## SRC-16 — gVisor overview

https://gvisor.dev/docs/

**Access:** Official documentation inspected. **Supports:** gVisor provides an application-kernel isolation approach integrated with container execution.

**Boundary:** Chosen Linux sandbox qualification target, not proof of sufficient security or universal host compatibility.

## SRC-17 — BCSC securities fundamentals

https://www.bcsc.bc.ca/industry/financial-technology-innovation/securities-law-fundamentals

**Access:** Official documentation inspected. **Supports:** Being in the business of advising about securities can engage registration requirements, subject to applicable exemptions.

**Boundary:** Counsel must classify this actual non-discretionary product per jurisdiction. A disclaimer is not clearance.

## SRC-18 — SEC investor bulletin on robo-advisers

https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-45

**Access:** Official regulator guidance inspected. **Supports:** Automated investment advice has securities-law considerations, including SEC/state adviser frameworks.

**Boundary:** Not a product-specific determination; map federal and relevant state obligations with counsel.

## SRC-19 — FINRA stop-order risks

https://www.finra.org/investors/insights/stop-orders-factors-consider-during-volatile-markets

**Access:** Official guidance inspected. **Supports:** A stop trigger does not guarantee the execution price, and stop-limit orders may not execute.

**Boundary:** Planned stop loss is a scenario, not guaranteed maximum loss.

## SRC-20 — OIC assignment

https://www.optionseducation.org/referencelibrary/faq/options-assignment

**Access:** Official industry education inspected. **Supports:** American-style short options can be assigned before expiry.

**Boundary:** Separate expiration payoff from pathwise collateral, assignment and settlement risk.

## SRC-21 — OIC vertical put spreads

https://www.optionseducation.org/strategies/all-strategies/bear-put-spread

**Access:** Official industry education inspected. **Supports:** Vertical-spread analysis includes early-assignment and financing consequences, not merely an expiry payoff.

**Boundary:** Do not model a spread as an inseparable atomic position throughout its life.

## SRC-22 — Probability of Backtest Overfitting

https://scholarworks.wmich.edu/math_pubs/42/

**Access:** Primary research landing page inspected. **Supports:** Bailey, Borwein, Lopez de Prado and Zhu discuss selection-related backtest overfitting and a diagnostic framework.

**Boundary:** Diagnostics inform research; no universal test proves a trading edge.

## SRC-23 — Apache License 2.0

https://www.apache.org/licenses/LICENSE-2.0.txt

**Access:** Official license text inspected. **Supports:** Chosen project license. Dependencies, datasets, prompts and model terms still require separate compatibility review.

**Boundary:** Copyright ownership/contributor policy must be finalized before public publication.

## SRC-24 — Codex AGENTS.md

https://learn.chatgpt.com/docs/agent-configuration/agents-md

**Access:** Official documentation inspected. **Supports:** AGENTS.md provides repository instructions with discovery/precedence rules.

**Boundary:** Verify actual client version and applicable instructions in the target repository.

## SRC-25 — Codex execution plans

https://developers.openai.com/cookbook/articles/codex_exec_plans

**Access:** Official documentation inspected. **Supports:** Persistent execution plans can specify goals, steps and verification evidence for longer work.

**Boundary:** This bundle supplies its own bounded task contracts; no external plugin is required.

## SRC-26 — Existing user AC/ECC compatibility guide

Library: Codex_AC_ECC_Setup.md, 2026-10-06

**Access:** User Library excerpt inspected. **Supports:** Existing guide uses AGENTS.md, CLAUDE.md and a shared .agentic state; configured checks must not be overwritten.

**Boundary:** This bundle is an original project-specific handoff, not the ECC distribution or an installer.
